"""Order Tracker — order lifecycle tracking with timeout handling.

Tracks orders dispatched through the Gateway:
- Status tracking: PENDING → PLACED → FILLED / REJECTED / TIMEOUT
- Timeout handling: orders exceeding max latency are flagged
- Execution records: logs fill price, commission, and timing
- Garbage collection: periodic cleanup of old terminal orders
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────

DEFAULT_ORDER_TIMEOUT_SEC = 30.0
DEFAULT_CLEANUP_INTERVAL = 300  # 5 minutes
DEFAULT_MAX_TRACKED_ORDERS = 10000


class TrackedOrderState(str, Enum):
    """Tracked order lifecycle states."""
    PENDING = "PENDING"
    PLACED = "PLACED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"


@dataclass
class TrackedOrder:
    """An order being tracked by the OrderTracker."""
    client_id: str
    signal_id: int = 0
    account_id: int = 0
    symbol: str = ""
    direction: str = ""
    lot: float = 0.0
    mt5_ticket: int = 0
    filled_price: float = 0.0
    entry_price: float = 0.0
    commission: float = 0.0
    state: TrackedOrderState = TrackedOrderState.PENDING
    latency_ms: int = 0
    error_message: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def age_seconds(self) -> float:
        """Age of the order in seconds."""
        return time.time() - self.created_at

    @property
    def is_terminal(self) -> bool:
        """Whether the order is in a terminal state."""
        return self.state in (
            TrackedOrderState.FILLED,
            TrackedOrderState.REJECTED,
            TrackedOrderState.TIMEOUT,
            TrackedOrderState.CANCELLED,
            TrackedOrderState.ERROR,
        )


@dataclass
class OrderTrackerStats:
    """Statistics for order tracking."""
    total_tracked: int = 0
    active: int = 0
    filled: int = 0
    rejected: int = 0
    timeout: int = 0
    cancelled: int = 0
    error: int = 0


class OrderTracker:
    """Tracks order lifecycle from placement to execution.

    Maintains an in-memory registry of dispatched orders with:
    - State transitions (PENDING → PLACED → FILLED)
    - Timeout detection (orders stuck in PENDING/PLACED)
    - Execution record logging (fill price, commission, latency)
    - Periodic cleanup of stale terminal orders

    Example:
        tracker = OrderTracker(order_timeout=30.0)
        await tracker.start()
        await tracker.track_order(
            client_id="disp-123", signal_id=456, account_id=6,
            symbol="XAUUSD", direction="BUY", lot=0.1,
            mt5_ticket=293852200, filled_price=4105.50,
        )
    """

    def __init__(
        self,
        order_timeout: float = DEFAULT_ORDER_TIMEOUT_SEC,
        cleanup_interval: int = DEFAULT_CLEANUP_INTERVAL,
        max_orders: int = DEFAULT_MAX_TRACKED_ORDERS,
        notifier: Any = None,
    ):
        """Initialize OrderTracker.

        Args:
            order_timeout: Max seconds before an order is considered timed out.
            cleanup_interval: Interval for stale order cleanup in seconds.
            max_orders: Maximum number of orders to track (prevents memory leak).
            notifier: Optional Notifier for order-lifecycle push (DingTalk/WeCom).
        """
        self._order_timeout = order_timeout
        self._cleanup_interval = cleanup_interval
        self._max_orders = max_orders
        self._notifier = notifier
        self._orders: dict[str, TrackedOrder] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._cleanup_task: Optional[asyncio.Task] = None
        self._timeout_check_task: Optional[asyncio.Task] = None

        # Statistics
        self._stats = OrderTrackerStats()

    # ── Lifecycle ───────────────────────────────

    async def start(self) -> None:
        """Start background cleanup and timeout detection tasks."""
        if self._running:
            return
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        self._timeout_check_task = asyncio.create_task(self._timeout_loop())
        logger.info(
            "OrderTracker started (timeout=%.1fs, cleanup=%ds)",
            self._order_timeout, self._cleanup_interval,
        )

    async def stop(self) -> None:
        """Stop background tasks."""
        self._running = False
        for task in [self._cleanup_task, self._timeout_check_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._cleanup_task = None
        self._timeout_check_task = None
        logger.info("OrderTracker stopped (stats=%s)", self.get_stats())

    # ── Track Order ─────────────────────────────

    async def track_order(
        self,
        client_id: str,
        signal_id: int,
        account_id: int,
        symbol: str,
        direction: str,
        lot: float,
        mt5_ticket: int = 0,
        filled_price: float = 0.0,
        entry_price: float = 0.0,
        commission: float = 0.0,
        latency_ms: int = 0,
    ) -> TrackedOrder:
        """Register a new order for tracking.

        Args:
            client_id: Unique client order ID.
            signal_id: Signal that generated this order.
            account_id: MT5 account ID.
            symbol: Trading symbol.
            direction: "BUY" or "SELL".
            lot: Trade volume.
            mt5_ticket: MT5 ticket number (0 if pending).
            filled_price: Fill price (0 if not yet filled).
            commission: Commission charged.
            latency_ms: End-to-end latency in ms.

        Returns:
            The created TrackedOrder.
        """
        async with self._lock:
            # Enforce max orders limit
            if len(self._orders) >= self._max_orders:
                # Remove oldest terminal orders
                terminal = [
                    cid for cid, o in self._orders.items() if o.is_terminal
                ]
                if terminal:
                    oldest = sorted(
                        terminal,
                        key=lambda cid: self._orders[cid].updated_at,
                    )[:max(1, len(terminal) // 2)]
                    for cid in oldest:
                        del self._orders[cid]
                        logger.debug("Evicted old order: %s", cid)

            order = TrackedOrder(
                client_id=client_id,
                signal_id=signal_id,
                account_id=account_id,
                symbol=symbol,
                direction=direction,
                lot=lot,
                mt5_ticket=mt5_ticket,
                filled_price=filled_price,
                entry_price=entry_price,
                commission=commission,
                state=TrackedOrderState.PLACED if mt5_ticket else TrackedOrderState.PENDING,
                latency_ms=latency_ms,
            )
            self._orders[client_id] = order
            self._stats.total_tracked += 1
            self._stats.active += 1

            # 下单通知: 进入 PLACED 时非阻塞广播 (钉钉/企业微信)。
            # 用快照 dict 传参, 避免后续 mark_filled 原地修改对象导致的竞态。
            if mt5_ticket and self._notifier is not None:
                try:
                    snap = {
                        "account_id": order.account_id,
                        "symbol": order.symbol,
                        "direction": order.direction,
                        "lot": order.lot,
                        "filled_price": order.filled_price,
                        "entry_price": order.entry_price,
                        "mt5_ticket": order.mt5_ticket,
                    }
                    asyncio.create_task(self._notifier.on_order(snap))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Order notification scheduling failed: %s", exc)

            if mt5_ticket:
                logger.debug(
                    "Order tracked: client_id=%s, ticket=%s, symbol=%s, "
                    "direction=%s, lot=%s, latency=%dms",
                    client_id, mt5_ticket, symbol, direction, lot, latency_ms,
                )
            else:
                logger.debug(
                    "Order tracked (pending): client_id=%s, signal_id=%s",
                    client_id, signal_id,
                )

            return order

    # ── State Updates ───────────────────────────

    async def update_state(
        self,
        client_id: str,
        new_state: TrackedOrderState,
        mt5_ticket: int = 0,
        filled_price: float = 0.0,
        commission: float = 0.0,
        error_message: str = "",
    ) -> Optional[TrackedOrder]:
        """Update the state of a tracked order.

        Args:
            client_id: Client order ID.
            new_state: New order state.
            mt5_ticket: MT5 ticket (if assigned).
            filled_price: Fill price.
            commission: Commission.
            error_message: Error description.

        Returns:
            Updated TrackedOrder or None if not found.
        """
        async with self._lock:
            order = self._orders.get(client_id)
            if order is None:
                logger.warning("Tracked order not found: client_id=%s", client_id)
                return None

            old_state = order.state
            order.state = new_state
            order.updated_at = time.time()

            if mt5_ticket:
                order.mt5_ticket = mt5_ticket
            if filled_price:
                order.filled_price = filled_price
            if commission:
                order.commission = commission
            if error_message:
                order.error_message = error_message

            # Update stats
            if old_state != new_state:
                if not order.is_terminal:
                    # Moving to non-terminal — no stat change
                    pass
                elif new_state == TrackedOrderState.FILLED:
                    self._stats.filled += 1
                    self._stats.active = max(0, self._stats.active - 1)
                elif new_state == TrackedOrderState.REJECTED:
                    self._stats.rejected += 1
                    self._stats.active = max(0, self._stats.active - 1)
                elif new_state == TrackedOrderState.TIMEOUT:
                    self._stats.timeout += 1
                    self._stats.active = max(0, self._stats.active - 1)
                elif new_state == TrackedOrderState.CANCELLED:
                    self._stats.cancelled += 1
                    self._stats.active = max(0, self._stats.active - 1)
                elif new_state == TrackedOrderState.ERROR:
                    self._stats.error += 1
                    self._stats.active = max(0, self._stats.active - 1)

            logger.info(
                "Order state transition: client_id=%s, %s → %s, ticket=%s",
                client_id, old_state.value, new_state.value, order.mt5_ticket,
            )
            return order

    async def mark_filled(
        self,
        client_id: str,
        filled_price: float,
        commission: float = 0.0,
    ) -> Optional[TrackedOrder]:
        """Mark an order as filled.

        Args:
            client_id: Client order ID.
            filled_price: Actual fill price.
            commission: Commission charged.

        Returns:
            Updated TrackedOrder or None.
        """
        return await self.update_state(
            client_id=client_id,
            new_state=TrackedOrderState.FILLED,
            filled_price=filled_price,
            commission=commission,
        )

    # ── Query ───────────────────────────────────

    async def get_order(self, client_id: str) -> Optional[TrackedOrder]:
        """Get a tracked order by client ID.

        Args:
            client_id: Client order ID.

        Returns:
            TrackedOrder or None.
        """
        return self._orders.get(client_id)

    async def get_active_orders(
        self,
        account_id: int = 0,
        symbol: str = "",
    ) -> list[TrackedOrder]:
        """Get all active (non-terminal) tracked orders.

        Args:
            account_id: Filter by account (0 = all).
            symbol: Filter by symbol (empty = all).

        Returns:
            List of active TrackedOrder objects.
        """
        async with self._lock:
            active = [
                o for o in self._orders.values()
                if not o.is_terminal
                and (account_id == 0 or o.account_id == account_id)
                and (not symbol or o.symbol == symbol)
            ]
            return sorted(active, key=lambda o: o.created_at, reverse=True)

    async def get_stats(self) -> dict:
        """Get tracker statistics.

        Returns:
            Dict with tracking statistics.
        """
        async with self._lock:
            return {
                "total_tracked": self._stats.total_tracked,
                "active": self._stats.active,
                "filled": self._stats.filled,
                "rejected": self._stats.rejected,
                "timeout": self._stats.timeout,
                "cancelled": self._stats.cancelled,
                "error": self._stats.error,
                "current_orders": len(self._orders),
            }

    # ── Background Tasks ────────────────────────

    async def _timeout_loop(self) -> None:
        """Periodically check for timed-out orders."""
        logger.info("OrderTracker timeout checker started")
        check_interval = min(5.0, self._order_timeout / 2)
        try:
            while self._running:
                await asyncio.sleep(check_interval)
                await self._check_timeouts()
        except asyncio.CancelledError:
            logger.info("OrderTracker timeout checker stopped")

    async def _check_timeouts(self) -> None:
        """Mark orders that have exceeded the timeout as TIMEOUT."""
        now = time.time()
        async with self._lock:
            for order in self._orders.values():
                if order.is_terminal:
                    continue
                if order.state in (
                    TrackedOrderState.PENDING,
                    TrackedOrderState.PLACED,
                ):
                    if (now - order.created_at) > self._order_timeout:
                        order.state = TrackedOrderState.TIMEOUT
                        order.updated_at = now
                        order.error_message = (
                            f"Order timed out after {self._order_timeout:.0f}s"
                        )
                        self._stats.timeout += 1
                        self._stats.active = max(0, self._stats.active - 1)
                        logger.warning(
                            "Order timeout: client_id=%s, signal_id=%s, "
                            "symbol=%s, age=%.1fs",
                            order.client_id, order.signal_id,
                            order.symbol, order.age_seconds,
                        )

    async def _cleanup_loop(self) -> None:
        """Periodically clean up stale terminal orders."""
        logger.info("OrderTracker cleanup loop started")
        try:
            while self._running:
                await asyncio.sleep(self._cleanup_interval)
                await self._cleanup_stale()
        except asyncio.CancelledError:
            logger.info("OrderTracker cleanup loop stopped")

    async def _cleanup_stale(self) -> int:
        """Remove terminal orders older than cleanup_interval.

        Returns:
            Number of orders cleaned up.
        """
        now = time.time()
        async with self._lock:
            stale_ids = [
                cid for cid, order in self._orders.items()
                if order.is_terminal
                and (now - order.updated_at) > self._cleanup_interval
            ]
            for cid in stale_ids:
                del self._orders[cid]

            if stale_ids:
                logger.debug("OrderTracker cleaned %d stale orders", len(stale_ids))
            return len(stale_ids)

    # ── Health ──────────────────────────────────

    async def health_check(self) -> dict:
        """Check tracker health.

        Returns:
            Dict with status and stats.
        """
        stats = await self.get_stats()
        return {
            "status": "healthy" if self._running else "stopped",
            "running": self._running,
            **stats,
        }
