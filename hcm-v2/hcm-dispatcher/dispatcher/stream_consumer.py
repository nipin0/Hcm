"""Dispatcher Stream Consumer — Redis Stream XREADGROUP consumer.

Consumes from signal:risk_passed using the dispatcher-group consumer group.
Features:
- XGROUP CREATE on startup (idempotent)
- Pending message recovery
- gRPC order placement via GatewayClient
- Order tracking via OrderTracker
- ACK on successful dispatch
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

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────

RISK_PASSED_STREAM = "signal:risk_passed"
DEAD_LETTER_STREAM = "signal:dead"
GROUP_NAME = "dispatcher-group"
DEFAULT_CONSUMER_NAME = "dispatcher-consumer-1"
DEFAULT_BLOCK_MS = 5000
DEFAULT_RETRY_MAX = 3
DEFAULT_RETRY_DELAY = 0.5


@dataclass
class DispatcherConsumerConfig:
    """Configuration for the dispatcher stream consumer."""
    group_name: str = GROUP_NAME
    consumer_name: str = DEFAULT_CONSUMER_NAME
    risk_passed_stream: str = RISK_PASSED_STREAM
    dead_stream: str = DEAD_LETTER_STREAM
    block_ms: int = DEFAULT_BLOCK_MS
    retry_max: int = DEFAULT_RETRY_MAX
    retry_delay: float = DEFAULT_RETRY_DELAY


class DispatcherStreamConsumer:
    """Redis Stream consumer that reads risk-passed signals and dispatches
    them to the Gateway via gRPC for order placement.

    Operates as part of the dispatcher-group consumer group on
    signal:risk_passed. Each message results in a gRPC PlaceOrder call
    to the Gateway, with the order tracked via OrderTracker.

    Example:
        consumer = DispatcherStreamConsumer(
            redis_client=redis_client,
            gateway_client=gateway_client,
            order_tracker=order_tracker,
        )
        await consumer.start()
    """

    def __init__(
        self,
        redis_client: RedisClient,
        gateway_client: Any = None,
        order_tracker: Any = None,
        db_pool: Any = None,
        config: Optional[DispatcherConsumerConfig] = None,
    ):
        """Initialize DispatcherStreamConsumer.

        Args:
            redis_client: Initialized RedisClient instance.
            gateway_client: GatewayClient for gRPC order placement.
            order_tracker: OrderTracker for order lifecycle tracking.
            db_pool: DatabasePool for updating signal_status after order.
            config: Optional consumer configuration.
        """
        self._redis = redis_client
        self._gateway = gateway_client
        self._tracker = order_tracker
        self._db = db_pool
        self._config = config or DispatcherConsumerConfig()
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Statistics
        self._stats: dict[str, int] = {
            "messages_consumed": 0,
            "orders_placed": 0,
            "orders_failed": 0,
            "orders_dead_letter": 0,
        }

    # ── Lifecycle ───────────────────────────────

    async def start(self) -> None:
        """Start the consumer loop as a background task."""
        if self._running:
            return

        # Ensure consumer group exists.
        # start_id="0" means: if the group is being created for the first
        # time, start consuming from the very first message in the stream.
        # This guarantees no risk-passed signal is missed even if published
        # before the dispatcher came online.
        await self._redis.xgroup_create(
            self._config.risk_passed_stream,
            self._config.group_name,
            mkstream=True,
            start_id="0",
        )

        self._running = True
        self._task = asyncio.create_task(self._consume_loop())
        logger.info(
            "DispatcherStreamConsumer started: group=%s, stream=%s",
            self._config.group_name, self._config.risk_passed_stream,
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
        logger.info("DispatcherStreamConsumer stopped (stats=%s)", self._stats)

    # ── Main Loop ───────────────────────────────

    async def _consume_loop(self) -> None:
        """Main consumption loop: XREADGROUP → place order → track → ACK."""
        logger.info(
            "DispatcherStreamConsumer loop started: consumer=%s",
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
                logger.error("DispatcherStreamConsumer loop error: %s", exc)
                await asyncio.sleep(1.0)

        logger.info("DispatcherStreamConsumer loop ended")

    # ── Message Processing ──────────────────────

    async def _process_message(self, msg: StreamMessage) -> None:
        """Process a risk-passed signal: place order via Gateway gRPC.

        Args:
            msg: StreamMessage from signal:risk_passed.
        """
        self._stats["messages_consumed"] += 1
        signal_id = msg.data.get("signal_id", 0)
        symbol = msg.data.get("symbol", "?")
        t0 = time.time()

        logger.debug(
            "Dispatching: signal_id=%s, symbol=%s, direction=%s",
            signal_id, symbol, msg.data.get("direction", ""),
        )

        success = False
        last_error: Optional[str] = None

        for attempt in range(1, self._config.retry_max + 1):
            try:
                # Extract order parameters from signal
                account_id = int(msg.data.get("account_id", 0))
                direction = msg.data.get("direction", "")

                # 拦截非法方向：只有 BUY/SELL 才进入下单通道。
                # NO_TRADE / 空 / MODIFY / PARTIAL_CLOSE 等"不下单"信号若被下发，
                # stub/回退路径会误判成功并把"No-trade 订单信息"推到钉钉（见 2026-07-20 审计）。
                # 这里直接 ACK 跳过，避免当订单 track + 推钉钉，也不进死信堆积。
                if direction.upper() not in ("BUY", "SELL"):
                    logger.warning(
                        "Skipping non-trade direction (not dispatched): "
                        "signal_id=%s, symbol=%s, direction=%s",
                        signal_id, symbol, direction,
                    )
                    await self._redis.xack(
                        self._config.risk_passed_stream,
                        self._config.group_name,
                        msg.message_id,
                    )
                    return
                lot = float(msg.data.get("lot", 0.0))
                if lot <= 0.0:
                    lot = 0.01
                sl_price = float(msg.data.get("sl_price", 0.0))
                tp1 = float(msg.data.get("tp1", 0.0))
                entry_price = float(msg.data.get("entry_price", 0.0))
                client_id = f"disp-{signal_id}-{int(time.time() * 1000)}"

                # Place order via Gateway gRPC
                if self._gateway is not None:
                    order_result = await self._gateway.place_order(
                        client_id=client_id,
                        account_id=account_id,
                        symbol=symbol,
                        direction=direction,
                        lot=lot,
                        sl=sl_price,
                        tp=tp1,
                        entry_price=entry_price,
                        order_type="MARKET",
                        comment=f"HCM_v2_signal_{signal_id}",
                    )
                else:
                    # Stub: simulate order placement
                    order_result = {
                        "code": 0,
                        "message": "ok (stub)",
                        "mt5_ticket": int(time.time() * 1000) % 1000000000,
                        "filled_price": entry_price,
                        "commission": 0.0,
                        "latency_ms": 0,
                    }

                latency_ms = int((time.time() - t0) * 1000)

                if order_result.get("code", 1) == 0:
                    self._stats["orders_placed"] += 1

                    # Track the order
                    if self._tracker is not None:
                        await self._tracker.track_order(
                            client_id=client_id,
                            signal_id=signal_id,
                            account_id=account_id,
                            symbol=symbol,
                            direction=direction,
                            lot=lot,
                            mt5_ticket=order_result.get("mt5_ticket", 0),
                            filled_price=order_result.get("filled_price", 0.0),
                            entry_price=entry_price,
                            commission=order_result.get("commission", 0.0),
                            latency_ms=latency_ms,
                        )

                    logger.info(
                        "Order placed: signal_id=%s, ticket=%s, symbol=%s, "
                        "direction=%s, lot=%s, latency=%dms",
                        signal_id, order_result.get("mt5_ticket"), symbol,
                        direction, lot, latency_ms,
                    )

                    # Update signal_status to 3 (已分发) in PostgreSQL
                    if self._db is not None and self._db.is_initialized:
                        try:
                            await self._db.execute(
                                "UPDATE hcm_signal.signals SET signal_status=3, "
                                "updated_at=now() WHERE signal_id=$1",
                                signal_id,
                            )
                        except Exception as db_exc:
                            logger.warning(
                                "Failed to update signal_status for %s: %s",
                                signal_id, db_exc,
                            )

                    success = True
                else:
                    last_error = order_result.get("message", "Unknown gateway error")
                    logger.warning(
                        "Gateway order failed (attempt %d/%d): signal_id=%s, error=%s",
                        attempt, self._config.retry_max, signal_id, last_error,
                    )

                if success:
                    break

            except Exception as exc:
                last_error = str(exc)
                logger.warning(
                    "Dispatch attempt %d/%d failed (signal_id=%s): %s",
                    attempt, self._config.retry_max, signal_id, exc,
                )

            if attempt < self._config.retry_max:
                await asyncio.sleep(self._config.retry_delay * attempt)

        # ACK or dead letter
        if success:
            await self._redis.xack(
                self._config.risk_passed_stream,
                self._config.group_name,
                msg.message_id,
            )
        else:
            self._stats["orders_failed"] += 1
            await self._send_to_dead_letter(msg, last_error or "dispatch failed")
            # ACK to prevent infinite retry
            await self._redis.xack(
                self._config.risk_passed_stream,
                self._config.group_name,
                msg.message_id,
            )

    # ── Dead Letter ─────────────────────────────

    async def _send_to_dead_letter(
        self,
        msg: StreamMessage,
        error: str,
    ) -> None:
        """Send a failed dispatch to the dead letter queue.

        Args:
            msg: Original StreamMessage.
            error: Error description.
        """
        self._stats["orders_dead_letter"] += 1
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
                "Dispatch dead letter: signal_id=%s, error=%s → %s",
                dead_data["original_signal_id"], error, self._config.dead_stream,
            )
        except Exception as exc:
            logger.critical(
                "CRITICAL: Cannot write to dead letter queue (signal_id=%s): %s",
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
                "Found %d pending dispatch messages — recovering", pending_count,
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
                    "Recovering pending dispatch: signal_id=%s",
                    msg.data.get("signal_id"),
                )
                await self._process_message(msg)

            remaining = await self._redis.xpending(
                self._config.risk_passed_stream,
                self._config.group_name,
            )
            if remaining > 0:
                logger.warning("%d pending dispatch messages remain", remaining)
            else:
                logger.info("All pending dispatch messages recovered")

        except Exception as exc:
            logger.error("Pending dispatch recovery failed: %s", exc)

    # ── Stats & Health ──────────────────────────

    async def get_stats(self) -> dict:
        """Get consumer statistics.

        Returns:
            Dict with consumption counts.
        """
        stats = dict(self._stats)
        if self._tracker is not None:
            stats["tracker"] = await self._tracker.get_stats()
        return stats

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
            "stats": await self.get_stats(),
        }
