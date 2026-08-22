"""Copy Trading Stream Consumer — Redis Stream XREADGROUP consumer.

Consumes from signal:risk_passed using the copy-trading-group consumer group.
Features:
- XGROUP CREATE on startup (idempotent)
- Pending message recovery with deduplication
- Symbol mapping via SymbolMapper (O(1) hash lookup)
- Lot calculation via LotCalculator (local cache)
- Order execution via OrderExecutor (gRPC <100ms target)
- ACK on successful copy
- Dead letter queue on persistent failure
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from shared.redis_client import RedisClient, StreamMessage
from shared.errors import ErrorCode

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────

RISK_PASSED_STREAM = "signal:risk_passed"
DEAD_LETTER_STREAM = "signal:dead"
GROUP_NAME = "copy-trading-group"
DEFAULT_CONSUMER_NAME = "copy-consumer-1"
DEFAULT_BLOCK_MS = 5000
DEFAULT_RETRY_MAX = 3
DEFAULT_RETRY_DELAY = 0.5

# Deduplication: track recently processed signal IDs
DEDUP_TTL = 3600  # 1 hour
DEDUP_KEY_PREFIX = "hcm:copytrade:dedup"


@dataclass
class CopyConsumerConfig:
    """Configuration for the copy trading stream consumer."""
    group_name: str = GROUP_NAME
    consumer_name: str = DEFAULT_CONSUMER_NAME
    risk_passed_stream: str = RISK_PASSED_STREAM
    dead_stream: str = DEAD_LETTER_STREAM
    block_ms: int = DEFAULT_BLOCK_MS
    retry_max: int = DEFAULT_RETRY_MAX
    retry_delay: float = DEFAULT_RETRY_DELAY
    dedup_ttl: int = DEDUP_TTL


class CopyTradingStreamConsumer:
    """Redis Stream consumer that reads risk-passed signals and executes
    copy trades via symbol mapping, lot calculation, and order execution.

    Operates as part of the copy-trading-group consumer group on
    signal:risk_passed. Each message goes through:
    1. Deduplication check
    2. Symbol mapping (master → follower)
    3. Lot calculation (configurable mode)
    4. Order execution (gRPC or Redis PUB)

    Performance target: <100ms end-to-end latency.

    Example:
        consumer = CopyTradingStreamConsumer(
            redis_client=redis_client,
            symbol_mapper=symbol_mapper,
            lot_calculator=lot_calculator,
            order_executor=order_executor,
        )
        await consumer.start()
    """

    def __init__(
        self,
        redis_client: RedisClient,
        symbol_mapper: Any = None,
        lot_calculator: Any = None,
        order_executor: Any = None,
        config: Optional[CopyConsumerConfig] = None,
        db_pool: Any = None,
    ):
        """Initialize CopyTradingStreamConsumer.

        Args:
            redis_client: Initialized RedisClient instance.
            symbol_mapper: SymbolMapper for master→follower symbol translation.
            lot_calculator: LotCalculator for lot size computation.
            order_executor: OrderExecutor for order placement.
            config: Optional consumer configuration.
            db_pool: DatabasePool for loading copy relationships.
        """
        self._redis = redis_client
        self._symbol_mapper = symbol_mapper
        self._lot_calculator = lot_calculator
        self._order_executor = order_executor
        self._db = db_pool
        self._config = config or CopyConsumerConfig()
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Copy configuration cache (account-level)
        self._copy_configs: dict[int, dict] = {}  # account_id → config

        # Statistics
        self._stats: dict[str, int] = {
            "messages_consumed": 0,
            "copies_executed": 0,
            "copies_skipped_duplicate": 0,
            "copies_skipped_no_mapping": 0,
            "copies_failed": 0,
            "copies_dead_letter": 0,
        }

    # ── Lifecycle ───────────────────────────────

    async def start(self) -> None:
        """Start the consumer loop and load copy configurations."""
        if self._running:
            return

        # Ensure consumer group exists
        await self._redis.xgroup_create(
            self._config.risk_passed_stream,
            self._config.group_name,
            mkstream=True,
            start_id="$",
        )

        # Load copy configurations from DB
        await self._load_copy_configs()

        self._running = True
        self._task = asyncio.create_task(self._consume_loop())
        logger.info(
            "CopyTradingStreamConsumer started: group=%s, stream=%s, accounts=%d",
            self._config.group_name, self._config.risk_passed_stream,
            len(self._copy_configs),
        )

    async def stop(self) -> None:
        """Gracefully stop the consumer loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("CopyTradingStreamConsumer stopped (stats=%s)", self._stats)

    # ── Main Loop ───────────────────────────────

    async def _consume_loop(self) -> None:
        """Main consumption loop: XREADGROUP → map → calculate → execute → ACK."""
        logger.info(
            "CopyTradingStreamConsumer loop started: consumer=%s",
            self._config.consumer_name,
        )

        # 1. Recover pending messages
        await self._recover_pending()

        # 2. Main consumption loop
        while self._running:
            try:
                messages = await self._redis.xreadgroup(
                    group=self._config.group_name,
                    consumer=self._config.consumer_name,
                    streams={self._config.risk_passed_stream: ">"},
                    count=1,
                    block=self._config.block_ms,
                )

                for msg in messages:
                    await self._process_message(msg)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("CopyTradingStreamConsumer loop error: %s", exc)
                await asyncio.sleep(1.0)

        logger.info("CopyTradingStreamConsumer loop ended")

    # ── Message Processing ──────────────────────

    async def _process_message(self, msg: StreamMessage) -> None:
        """Process a risk-passed signal: copy trade to follower accounts.

        Pipeline:
        1. Deduplication check
        2. For each follower account: map symbol → calculate lot → execute

        Args:
            msg: StreamMessage from signal:risk_passed.
        """
        self._stats["messages_consumed"] += 1
        signal_id = int(msg.data.get("signal_id", 0))
        symbol = msg.data.get("symbol", "")
        t0 = time.time()

        logger.debug(
            "Copy trading: signal_id=%s, symbol=%s, direction=%s",
            signal_id, symbol, msg.data.get("direction", ""),
        )

        # ── manual_mirror 信号由 follower 桥直接执行（见 mt5_bridge 的 manual_mirror 分支），
        #    copy-trading 不参与，避免 OrderExecutor 对 CLOSE/MODIFY/PARTIAL_CLOSE/ADD 下错单或双下。
        #    此处仅 ACK，不做去重标记、不调用 OrderExecutor。
        if msg.data.get("signal_mode") == "manual_mirror":
            await self._redis.xack(
                self._config.risk_passed_stream,
                self._config.group_name,
                msg.message_id,
            )
            return

        # ── 方向合法性校验：仅 BUY/SELL 可下单；NO_TRADE/HOLD/空方向等
        #    被否决信号一律 ACK 跳过，避免误下错单（与 dispatcher 拦截同源）。
        direction = (msg.data.get("direction") or "").upper()
        if direction not in ("BUY", "SELL"):
            logger.warning(
                "Non-tradable direction (%s) for signal_id=%s, skip",
                msg.data.get("direction"), signal_id,
            )
            await self._redis.xack(
                self._config.risk_passed_stream,
                self._config.group_name,
                msg.message_id,
            )
            return

        # Deduplication check
        if await self._is_duplicate(signal_id):
            self._stats["copies_skipped_duplicate"] += 1
            logger.debug("Duplicate signal filtered: signal_id=%s", signal_id)
            await self._redis.xack(
                self._config.risk_passed_stream,
                self._config.group_name,
                msg.message_id,
            )
            return

        # Process each follower account
        master_account_id = int(msg.data.get("account_id", 0))
        overall_success = True

        for follower_account_id, copy_config in self._copy_configs.items():
            # Skip if copy config doesn't match master account
            if copy_config.get("master_account_id", 0) != master_account_id:
                continue

            success = False
            last_error: Optional[str] = None

            for attempt in range(1, self._config.retry_max + 1):
                try:
                    # 1. Symbol mapping (fallback to same symbol if no mapping)
                    if self._symbol_mapper is not None:
                        mapped = await self._symbol_mapper.map_symbol(
                            symbol=symbol,
                            master_account_id=master_account_id,
                            follower_account_id=follower_account_id,
                        )
                        follower_symbol = mapped if mapped is not None else symbol
                    else:
                        follower_symbol = symbol

                    # 2. Lot calculation
                    if self._lot_calculator is not None:
                        follower_lot = await self._lot_calculator.calculate(
                            account_id=follower_account_id,
                            symbol=follower_symbol,
                            signal_data=msg.data,
                            copy_config=copy_config,
                        )
                    else:
                        follower_lot = float(msg.data.get("lot", 0.1))

                    if follower_lot <= 0:
                        logger.warning(
                            "Calculated lot <= 0 for account_id=%s, signal_id=%s",
                            follower_account_id, signal_id,
                        )
                        success = True
                        break

                    # 3. Order execution
                    if self._order_executor is not None:
                        exec_result = await self._order_executor.execute(
                            account_id=follower_account_id,
                            symbol=follower_symbol,
                            signal_data=msg.data,
                            lot=follower_lot,
                            copy_config=copy_config,
                        )
                    else:
                        exec_result = {
                            "code": 0,
                            "message": "ok (stub)",
                            "mt5_ticket": int(time.time() * 1000) % 1000000000,
                            "latency_ms": 0,
                        }

                    if exec_result.get("code", 1) == 0:
                        self._stats["copies_executed"] += 1
                        success = True
                        logger.info(
                            "Copy executed: signal_id=%s, account=%d, "
                            "symbol=%s→%s, lot=%s, ticket=%s",
                            signal_id, follower_account_id, symbol,
                            follower_symbol, follower_lot,
                            exec_result.get("mt5_ticket"),
                        )
                    else:
                        last_error = exec_result.get("message", "Unknown")

                    if success:
                        break

                except Exception as exc:
                    last_error = str(exc)
                    logger.warning(
                        "Copy attempt %d/%d failed (signal_id=%s, account=%d): %s",
                        attempt, self._config.retry_max, signal_id, follower_account_id, exc,
                    )

                if attempt < self._config.retry_max:
                    await asyncio.sleep(self._config.retry_delay * attempt)

            if not success:
                self._stats["copies_failed"] += 1
                overall_success = False
                logger.error(
                    "Copy failed after %d retries: signal_id=%s, account=%d, error=%s",
                    self._config.retry_max, signal_id, follower_account_id, last_error,
                )

        # Mark as processed
        await self._mark_processed(signal_id)

        # ACK or dead letter
        if overall_success:
            await self._redis.xack(
                self._config.risk_passed_stream,
                self._config.group_name,
                msg.message_id,
            )
        else:
            self._stats["copies_dead_letter"] += 1
            await self._send_to_dead_letter(msg, "copy execution partially failed")
            await self._redis.xack(
                self._config.risk_passed_stream,
                self._config.group_name,
                msg.message_id,
            )

        latency_ms = int((time.time() - t0) * 1000)
        logger.debug(
            "Copy processing done: signal_id=%s, latency=%dms, success=%s",
            signal_id, latency_ms, overall_success,
        )

    # ── Deduplication ───────────────────────────

    async def _is_duplicate(self, signal_id: int) -> bool:
        """Check if a signal has already been processed.

        Uses Redis SET with NX + TTL for distributed dedup.

        Args:
            signal_id: Signal ID to check.

        Returns:
            True if already processed (duplicate).
        """
        if self._redis is None or not self._redis.is_initialized:
            return False

        try:
            dedup_key = f"{DEDUP_KEY_PREFIX}:{signal_id}"
            # SET NX: returns True if key didn't exist and was set
            was_set = await self._redis.raw.set(
                dedup_key, "1", nx=True, ex=self._config.dedup_ttl,
            )
            return not was_set  # True = already existed = duplicate
        except Exception as exc:
            logger.warning("Dedup check failed for signal_id=%s: %s", signal_id, exc)
            return False

    async def _mark_processed(self, signal_id: int) -> None:
        """Ensure a signal is marked as processed in dedup set.

        Args:
            signal_id: Signal ID to mark.
        """
        if self._redis is None or not self._redis.is_initialized:
            return
        try:
            dedup_key = f"{DEDUP_KEY_PREFIX}:{signal_id}"
            await self._redis.raw.setex(dedup_key, self._config.dedup_ttl, "1")
        except Exception:
            pass

    # ── Copy Config Loading ─────────────────────

    async def _load_copy_configs(self) -> None:
        """Load copy trading configurations from PostgreSQL.

        Reads follower account copy settings: master mapping, lot mode,
        risk parameters, etc.
        """
        if self._db is None or not self._db.is_initialized:
            logger.warning("No DB available — copy configs empty")
            return

        try:
            rows = await self._db.fetch(
                "SELECT r.relationship_id, r.master_account_id, r.copy_account_id AS follower_account_id, "
                "r.lot_mode, r.lot_multiplier, "
                "COALESCE(r.min_lot, 0.01) AS min_lot, COALESCE(r.max_lot, 5.0) AS max_lot, "
                "r.direction_mode, r.copy_sl, r.copy_tp, "
                "r.max_daily_loss, r.max_consecutive_losses, "
                "CASE WHEN r.status = 'running' THEN true ELSE false END AS is_active "
                "FROM hcm_copy.relationships r "
                "WHERE r.status = 'running' AND r.master_account_id IS NOT NULL AND r.copy_account_id IS NOT NULL"
            )

            self._copy_configs.clear()
            for row in rows:
                follower_id = row["follower_account_id"]
                self._copy_configs[follower_id] = {
                    "follower_account_id": follower_id,
                    "master_account_id": row["master_account_id"],
                    "lot_mode": row["lot_mode"] or "multiplier",
                    "fixed_lot": 0.1,
                    "lot_multiplier": float(row["lot_multiplier"] or 1.0),
                    "risk_percent": 1.0,
                    "balance_ratio": 0.01,
                    "equity_ratio": 0.01,
                    "min_lot": float(row["min_lot"] or 0.01),
                    "max_lot": float(row["max_lot"] or 5.0),
                    "sl_mode": "COPY",
                    "sl_offset_pips": 0,
                    "tp_mode": "COPY",
                    "tp_offset_pips": 0,
                    "max_slippage": 10,
                    "is_active": row["is_active"],
                }

            logger.info(
                "Loaded %d copy trading configurations", len(self._copy_configs),
            )
        except Exception as exc:
            logger.warning("Failed to load copy configs: %s — using empty configs", exc)

    # ── Dead Letter ─────────────────────────────

    async def _send_to_dead_letter(
        self,
        msg: StreamMessage,
        error: str,
    ) -> None:
        """Send a failed copy trade to the dead letter queue.

        Args:
            msg: Original StreamMessage.
            error: Error description.
        """
        dead_data = {
            "original_signal_id": msg.data.get("signal_id", 0),
            "symbol": msg.data.get("symbol", "?"),
            "direction": msg.data.get("direction", "?"),
            "failed_consumer": self._config.group_name,
            "error": error,
            "retry_count": self._config.retry_max,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "original_stream": msg.stream,
            "original_payload": json.dumps(msg.data, default=str),
        }

        try:
            await self._redis.xadd(self._config.dead_stream, dead_data)
            logger.error(
                "Copy trade dead letter: signal_id=%s, error=%s → %s",
                dead_data["original_signal_id"], error, self._config.dead_stream,
            )
        except Exception as exc:
            logger.critical(
                "CRITICAL: Cannot write to dead letter (signal_id=%s): %s",
                dead_data["original_signal_id"], exc,
            )

    # ── Pending Recovery ────────────────────────

    async def _recover_pending(self) -> None:
        """Recover and reprocess pending messages from previous crashes."""
        try:
            pending_count = await self._redis.xpending(
                self._config.risk_passed_stream,
                self._config.group_name,
            )
            if pending_count == 0:
                return

            logger.warning(
                "Found %d pending copy messages — recovering", pending_count,
            )

            messages = await self._redis.xreadgroup(
                group=self._config.group_name,
                consumer=self._config.consumer_name,
                streams={self._config.risk_passed_stream: "0"},
                count=min(pending_count, 50),
                block=1000,
            )

            for msg in messages:
                logger.info(
                    "Recovering pending copy: signal_id=%s",
                    msg.data.get("signal_id"),
                )
                await self._process_message(msg)

            remaining = await self._redis.xpending(
                self._config.risk_passed_stream,
                self._config.group_name,
            )
            if remaining > 0:
                logger.warning("%d pending copy messages remain", remaining)
            else:
                logger.info("All pending copy messages recovered")

        except Exception as exc:
            logger.error("Pending copy recovery failed: %s", exc)

    # ── Stats & Health ──────────────────────────

    def get_stats(self) -> dict:
        """Get consumer statistics.

        Returns:
            Dict with consumption counts.
        """
        return dict(self._stats)

    async def health_check(self) -> dict:
        """Check consumer health.

        Returns:
            Dict with status and stats.
        """
        redis_ok = False
        if self._redis.is_initialized:
            try:
                redis_ok = await self._redis.ping()
            except Exception:
                pass

        pending = 0
        if redis_ok:
            try:
                pending = await self._redis.xpending(
                    self._config.risk_passed_stream,
                    self._config.group_name,
                )
            except Exception:
                pass

        return {
            "status": "healthy" if redis_ok and self._running else "degraded",
            "redis_ok": redis_ok,
            "running": self._running,
            "pending_messages": pending,
            "copy_configs_loaded": len(self._copy_configs),
            "stats": self.get_stats(),
        }
