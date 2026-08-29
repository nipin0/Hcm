"""Order Executor — copy trade order execution via gRPC or Redis PUB.

Executes copy trades with two dispatch modes:
1. gRPC direct: Calls GatewayClient.place_order (primary, <50ms target)
2. Redis PUB: Publishes signal copy to hcm:copytrade:order channel (fallback)

Performance target: <50ms per copy trade execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────

COPY_ORDER_CHANNEL = "hcm:copytrade:order"
DEFAULT_MAGIC_PREFIX = 9000  # Copy trading magic numbers: 9000-9999
DEFAULT_RETRY_MAX = 2
DEFAULT_RETRY_DELAY = 0.1


class ExecutionMode(str, Enum):
    """Order execution modes."""
    GRPC = "GRPC"
    PUBSUB = "PUBSUB"


@dataclass
class ExecutionResult:
    """Result of an order execution."""
    code: int = 0
    message: str = "ok"
    mt5_ticket: int = 0
    filled_price: float = 0.0
    commission: float = 0.0
    latency_ms: int = 0
    mode: str = "GRPC"


class OrderExecutor:
    """Executes copy trade orders via gRPC (primary) or Redis PUB (fallback).

    Supports two execution modes:
    - GRPC: Direct gRPC call to Gateway. Low latency (<50ms).
    - PUBSUB: Publish to Redis channel for async execution.

    Example:
        executor = OrderExecutor(gateway_client=gateway_client, redis_client=redis_client)
        result = await executor.execute(
            account_id=6, symbol="XAUUSD", signal_data={...},
            lot=0.2, copy_config={"max_slippage": 10},
        )
    """

    def __init__(
        self,
        gateway_client: Any = None,
        redis_client: Any = None,
        retry_max: int = DEFAULT_RETRY_MAX,
        retry_delay: float = DEFAULT_RETRY_DELAY,
    ):
        """Initialize OrderExecutor.

        Args:
            gateway_client: GatewayClient for gRPC order placement.
            redis_client: RedisClient for PUB/SUB fallback.
            retry_max: Max retries per order.
            retry_delay: Delay between retries.
        """
        self._gateway = gateway_client
        self._redis = redis_client
        self._retry_max = retry_max
        self._retry_delay = retry_delay

        # Statistics
        self._stats: dict[str, int] = {
            "orders_grpc": 0,
            "orders_pubsub": 0,
            "orders_failed": 0,
            "total_latency_ms": 0,
        }

    # ── Main API ────────────────────────────────

    async def execute(
        self,
        account_id: int,
        symbol: str,
        signal_data: dict,
        lot: float,
        copy_config: dict,
    ) -> dict:
        """Execute a copy trade order.

        Tries gRPC first, falls back to Redis PUB on failure.

        Args:
            account_id: Follower MT5 account ID.
            symbol: Follower trading symbol (already mapped).
            signal_data: Original signal data dict.
            lot: Calculated follower lot size.
            copy_config: Copy configuration dict.

        Returns:
            Dict with code, message, mt5_ticket, latency_ms, mode.
        """
        t0 = time.time()

        direction = signal_data.get("direction", "BUY")
        sl_price = float(signal_data.get("sl_price", 0))
        tp_price = float(signal_data.get("tp1", 0))
        entry_price = float(signal_data.get("entry_price", 0))
        signal_id = int(signal_data.get("signal_id", 0))

        # Apply SL/TP mode adjustments
        sl_price = self._adjust_sl(
            sl_price, copy_config.get("sl_mode", "COPY"),
            float(copy_config.get("sl_offset_pips", 0)),
        )
        tp_price = self._adjust_tp(
            tp_price, copy_config.get("tp_mode", "COPY"),
            float(copy_config.get("tp_offset_pips", 0)),
        )

        # Generate client_id for idempotency.
        # 【2026-08-28 P1-13】去掉毫秒时间戳：原 client_id 每次重试/每次构造都不同，
        # 使下游无法据其做幂等去重 —— gRPC 已成交但回包超时/丢包时，PUB/SUB 兜底
        # 路径会再下一单 → 跟单账号重复开仓。改为仅由 (account_id, signal_id) 构成，
        # 使同一信号的重试与兜底路径复用同一幂等键（下游可按 client_id 去重）。
        client_id = f"copy-{account_id}-{signal_id}"

        # Compute magic number
        magic = DEFAULT_MAGIC_PREFIX + (account_id % 1000)

        slippage = int(copy_config.get("max_slippage", 10))

        # Try gRPC execution
        result = await self._execute_grpc(
            client_id=client_id,
            account_id=account_id,
            symbol=symbol,
            direction=direction,
            lot=lot,
            sl=sl_price,
            tp=tp_price,
            magic=magic,
            entry_price=entry_price,
            slippage=slippage,
            signal_id=signal_id,
        )

        latency_ms = int((time.time() - t0) * 1000)
        result["latency_ms"] = latency_ms
        self._stats["total_latency_ms"] += latency_ms

        if result.get("code", 1) != 0:
            # Fallback to PUB/SUB
            logger.warning(
                "gRPC execution failed, falling back to PUB/SUB: signal_id=%s, account=%d",
                signal_id, account_id,
            )
            result = await self._execute_pubsub(
                client_id=client_id,
                account_id=account_id,
                symbol=symbol,
                direction=direction,
                lot=lot,
                sl=sl_price,
                tp=tp_price,
                magic=magic,
                entry_price=entry_price,
                slippage=slippage,
                signal_id=signal_id,
            )
            result["latency_ms"] = latency_ms

        return result

    # ── gRPC Execution ──────────────────────────

    async def _execute_grpc(
        self,
        client_id: str,
        account_id: int,
        symbol: str,
        direction: str,
        lot: float,
        sl: float,
        tp: float,
        magic: int,
        entry_price: float,
        slippage: int,
        signal_id: int,
    ) -> dict:
        """Execute order via Gateway gRPC.

        Args:
            client_id: Unique order ID.
            account_id: MT5 account.
            symbol: Trading symbol.
            direction: BUY or SELL.
            lot: Trade volume.
            sl: Stop loss.
            tp: Take profit.
            magic: Magic number.
            entry_price: Entry price.
            slippage: Max slippage.
            signal_id: Source signal ID.

        Returns:
            Dict with execution result.
        """
        if self._gateway is None:
            return {"code": 99, "message": "No GatewayClient available", "mt5_ticket": 0, "mode": "GRPC"}

        for attempt in range(1, self._retry_max + 1):
            try:
                result = await self._gateway.place_order(
                    client_id=client_id,
                    account_id=account_id,
                    symbol=symbol,
                    direction=direction,
                    lot=lot,
                    sl=sl,
                    tp=tp,
                    magic=magic,
                    comment=f"HCM_Copy_signal_{signal_id}",
                    order_type="MARKET",
                    entry_price=entry_price,
                    slippage=slippage,
                )

                result["mode"] = "GRPC"

                if result.get("code", 1) == 0:
                    self._stats["orders_grpc"] += 1
                    logger.debug(
                        "gRPC order executed: ticket=%s, signal_id=%s, account=%d",
                        result.get("mt5_ticket"), signal_id, account_id,
                    )
                    return result

                logger.warning(
                    "gRPC attempt %d/%d failed: signal_id=%s, error=%s",
                    attempt, self._retry_max, signal_id,
                    result.get("message", "Unknown"),
                )

            except Exception as exc:
                logger.warning(
                    "gRPC attempt %d/%d exception: signal_id=%s, error=%s",
                    attempt, self._retry_max, signal_id, exc,
                )

            if attempt < self._retry_max:
                await asyncio.sleep(self._retry_delay * attempt)

        self._stats["orders_failed"] += 1
        return {
            "code": 99,
            "message": f"gRPC execution failed after {self._retry_max} attempts",
            "mt5_ticket": 0,
            "mode": "GRPC",
        }

    # ── PUB/SUB Execution ───────────────────────

    async def _execute_pubsub(
        self,
        client_id: str,
        account_id: int,
        symbol: str,
        direction: str,
        lot: float,
        sl: float,
        tp: float,
        magic: int,
        entry_price: float,
        slippage: int,
        signal_id: int,
    ) -> dict:
        """Execute order via Redis PUB/SUB (fallback).

        Publishes to hcm:copytrade:order channel for async processing.

        Args:
            Same as _execute_grpc.

        Returns:
            Dict with execution result.
        """
        if self._redis is None or not self._redis.is_initialized:
            return {
                "code": 99,
                "message": "No Redis available for PUB/SUB fallback",
                "mt5_ticket": 0,
                "mode": "PUBSUB",
            }

        order_data = {
            "client_id": client_id,
            "account_id": account_id,
            "symbol": symbol,
            "direction": direction,
            "lot": lot,
            "sl": sl,
            "tp": tp,
            "magic": magic,
            "entry_price": entry_price,
            "slippage": slippage,
            "comment": f"HCM_Copy_signal_{signal_id}",
            "order_type": "MARKET",
            "signal_id": signal_id,
            "timestamp": time.time(),
        }

        try:
            subscribers = await self._redis.publish(
                COPY_ORDER_CHANNEL, json.dumps(order_data),
            )
            self._stats["orders_pubsub"] += 1
            logger.info(
                "Copy order published via PUB/SUB: signal_id=%s, account=%d, "
                "subscribers=%d",
                signal_id, account_id, subscribers,
            )
            return {
                "code": 0,
                "message": f"Order queued via PUB/SUB ({subscribers} subscribers)",
                "mt5_ticket": 0,  # Async — ticket not yet known
                "mode": "PUBSUB",
            }
        except Exception as exc:
            self._stats["orders_failed"] += 1
            logger.error("PUB/SUB order publish failed: signal_id=%s, error=%s", signal_id, exc)
            return {
                "code": 99,
                "message": f"PUB/SUB publish failed: {exc}",
                "mt5_ticket": 0,
                "mode": "PUBSUB",
            }

    # ── SL/TP Adjustment ────────────────────────

    @staticmethod
    def _adjust_sl(sl_price: float, sl_mode: str, sl_offset: float) -> float:
        """Adjust stop loss price based on copy mode.

        Args:
            sl_price: Original stop loss.
            sl_mode: "COPY", "OFFSET", or "NONE".
            sl_offset: Offset in pips.

        Returns:
            Adjusted SL price.
        """
        if sl_mode == "COPY":
            return sl_price
        elif sl_mode == "OFFSET" and sl_price > 0:
            return round(sl_price + sl_offset, 5)
        elif sl_mode == "NONE":
            return 0.0
        return sl_price

    @staticmethod
    def _adjust_tp(tp_price: float, tp_mode: str, tp_offset: float) -> float:
        """Adjust take profit price based on copy mode.

        Args:
            tp_price: Original take profit.
            tp_mode: "COPY", "OFFSET", or "NONE".
            tp_offset: Offset in pips.

        Returns:
            Adjusted TP price.
        """
        if tp_mode == "COPY":
            return tp_price
        elif tp_mode == "OFFSET" and tp_price > 0:
            return round(tp_price + tp_offset, 5)
        elif tp_mode == "NONE":
            return 0.0
        return tp_price

    # ── Stats & Health ──────────────────────────

    def get_stats(self) -> dict:
        """Get executor statistics.

        Returns:
            Dict with execution counts and avg latency.
        """
        total = self._stats["orders_grpc"] + self._stats["orders_pubsub"]
        avg_latency = 0
        if total > 0:
            avg_latency = self._stats["total_latency_ms"] / total
        return {
            **self._stats,
            "avg_latency_ms": round(avg_latency, 2),
        }

    async def health_check(self) -> dict:
        """Check executor health.

        Returns:
            Dict with status and stats.
        """
        gateway_ok = False
        if self._gateway is not None:
            try:
                hc = await self._gateway.health_check()
                gateway_ok = hc.get("status", "").startswith("healthy")
            except Exception:
                pass

        redis_ok = False
        if self._redis is not None and self._redis.is_initialized:
            try:
                redis_ok = await self._redis.ping()
            except Exception:
                pass

        return {
            "status": "healthy" if (gateway_ok or redis_ok) else "degraded",
            "gateway_ok": gateway_ok,
            "redis_ok": redis_ok,
            "stats": self.get_stats(),
        }
