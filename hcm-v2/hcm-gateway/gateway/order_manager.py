"""Order Manager — order caching and state machine.

Manages the lifecycle of orders placed through the gateway, tracking
state transitions and providing query capabilities.

Order state machine:
    PENDING → PLACED → FILLED
                    → PARTIALLY_FILLED
                    → REJECTED
                    → CANCELLED
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

DEFAULT_CACHE_TTL = 3600  # 1 hour
DEFAULT_CLEANUP_INTERVAL = 300  # 5 minutes


class OrderState(str, Enum):
    """Order lifecycle states."""
    PENDING = "PENDING"
    PLACED = "PLACED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    ERROR = "ERROR"


class OrderType(str, Enum):
    """Order type constants."""
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


@dataclass
class OrderRecord:
    """Cached order record with full lifecycle data."""
    client_id: str
    account_id: int = 0
    symbol: str = ""
    direction: str = ""
    lot: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    magic: int = 0
    comment: str = ""
    order_type: str = "MARKET"
    entry_price: float = 0.0
    slippage: int = 10

    state: OrderState = OrderState.PENDING
    mt5_ticket: int = 0
    filled_price: float = 0.0
    commission: float = 0.0
    error_message: str = ""
    error_code: int = 0

    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    latency_ms: float = 0.0

    @property
    def age_seconds(self) -> float:
        """Seconds since order creation."""
        return time.time() - self.created_at

    @property
    def is_terminal(self) -> bool:
        """Whether the order has reached a terminal state."""
        return self.state in (
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
            OrderState.ERROR,
        )


class OrderManager:
    """Manages order lifecycle, caching, and querying.

    Maintains an in-memory cache of recent orders with TTL-based
    cleanup. Provides thread-safe access for concurrent gRPC handlers.

    Example:
        manager = OrderManager(cache_ttl=3600)
        order = await manager.create_order(
            client_id="abc-123", account_id=6, symbol="XAUUSD",
            direction="SELL", lot=0.02, sl=4106.70, tp=4084.10,
        )
        await manager.update_state(order.client_id, OrderState.FILLED, mt5_ticket=293852200)
    """

    def __init__(
        self,
        cache_ttl: int = DEFAULT_CACHE_TTL,
        cleanup_interval: int = DEFAULT_CLEANUP_INTERVAL,
    ):
        """Initialize OrderManager.

        Args:
            cache_ttl: Time-to-live for cached orders in seconds.
            cleanup_interval: Interval for stale order cleanup in seconds.
        """
        self._cache_ttl = cache_ttl
        self._cleanup_interval = cleanup_interval
        self._orders: dict[str, OrderRecord] = {}
        self._ticket_index: dict[int, str] = {}  # mt5_ticket → client_id
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._running = False

    # ── Lifecycle ───────────────────────────────

    async def start(self) -> None:
        """Start the cleanup background task."""
        if self._running:
            return
        self._running = True
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("OrderManager started (cache_ttl=%ds, cleanup_interval=%ds)",
                    self._cache_ttl, self._cleanup_interval)

    async def stop(self) -> None:
        """Stop the cleanup task and clear cache."""
        self._running = False
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
        logger.info("OrderManager stopped")

    # ── Order CRUD ──────────────────────────────

    async def create_order(
        self,
        client_id: str,
        account_id: int,
        symbol: str,
        direction: str,
        lot: float,
        sl: float = 0.0,
        tp: float = 0.0,
        magic: int = 0,
        comment: str = "",
        order_type: str = "MARKET",
        entry_price: float = 0.0,
        slippage: int = 10,
    ) -> OrderRecord:
        """Create a new order record in PENDING state.

        Args:
            client_id: Unique client-generated order ID (idempotency key).
            account_id: MT5 account ID.
            symbol: Trading symbol.
            direction: "BUY" or "SELL".
            lot: Trade volume in lots.
            sl: Stop loss price.
            tp: Take profit price.
            magic: EA magic number.
            comment: Order comment.
            order_type: "MARKET", "LIMIT", or "STOP".
            entry_price: Entry price for limit/stop orders.
            slippage: Maximum slippage in points.

        Returns:
            Newly created OrderRecord.

        Raises:
            ValueError: If client_id already exists.
        """
        async with self._lock:
            if client_id in self._orders:
                existing = self._orders[client_id]
                if not existing.is_terminal:
                    raise ValueError(f"Duplicate client_id: {client_id} (state={existing.state})")
                # Allow reuse if previous order is terminal
                logger.debug("Reusing client_id=%s (previous state=%s)", client_id, existing.state)

            order = OrderRecord(
                client_id=client_id,
                account_id=account_id,
                symbol=symbol,
                direction=direction.upper(),
                lot=lot,
                sl=sl,
                tp=tp,
                magic=magic,
                comment=comment,
                order_type=order_type.upper(),
                entry_price=entry_price,
                slippage=slippage,
            )
            self._orders[client_id] = order
            logger.info(
                "Order created: client_id=%s, symbol=%s, direction=%s, lot=%s, type=%s",
                client_id, symbol, direction, lot, order_type,
            )
            return order

    async def update_state(
        self,
        client_id: str,
        new_state: OrderState,
        mt5_ticket: int = 0,
        filled_price: float = 0.0,
        commission: float = 0.0,
        error_message: str = "",
        error_code: int = 0,
        latency_ms: float = 0.0,
    ) -> Optional[OrderRecord]:
        """Update the state of an existing order.

        Args:
            client_id: Client order ID.
            new_state: New order state.
            mt5_ticket: MT5 ticket number (for PLACED/FILLED).
            filled_price: Actual fill price.
            commission: Commission charged.
            error_message: Error description for REJECTED/ERROR.
            error_code: Error code for REJECTED/ERROR.
            latency_ms: Processing latency in milliseconds.

        Returns:
            Updated OrderRecord or None if not found.
        """
        async with self._lock:
            order = self._orders.get(client_id)
            if order is None:
                logger.warning("Order not found for update: client_id=%s", client_id)
                return None

            old_state = order.state
            order.state = new_state
            order.updated_at = time.time()

            if mt5_ticket:
                order.mt5_ticket = mt5_ticket
                self._ticket_index[mt5_ticket] = client_id
            if filled_price:
                order.filled_price = filled_price
            if commission:
                order.commission = commission
            if error_message:
                order.error_message = error_message
            if error_code:
                order.error_code = error_code
            if latency_ms:
                order.latency_ms = latency_ms

            logger.info(
                "Order state transition: client_id=%s, %s → %s, ticket=%d, latency=%.1fms",
                client_id, old_state.value, new_state.value, mt5_ticket, latency_ms,
            )
            return order

    async def get_order(self, client_id: str) -> Optional[OrderRecord]:
        """Get an order by client ID.

        Args:
            client_id: Client order ID.

        Returns:
            OrderRecord or None if not found.
        """
        return self._orders.get(client_id)

    async def get_order_by_ticket(self, mt5_ticket: int) -> Optional[OrderRecord]:
        """Get an order by MT5 ticket number.

        Args:
            mt5_ticket: MT5 order ticket.

        Returns:
            OrderRecord or None if not found.
        """
        client_id = self._ticket_index.get(mt5_ticket)
        if client_id:
            return self._orders.get(client_id)
        return None

    async def cancel_order(self, client_id: str) -> Optional[OrderRecord]:
        """Cancel a pending order.

        Args:
            client_id: Client order ID.

        Returns:
            Updated OrderRecord or None if not found.
        """
        async with self._lock:
            order = self._orders.get(client_id)
            if order is None:
                return None
            if order.is_terminal:
                logger.warning("Cannot cancel terminal order: client_id=%s, state=%s",
                             client_id, order.state.value)
                return order

            order.state = OrderState.CANCELLED
            order.updated_at = time.time()
            logger.info("Order cancelled: client_id=%s", client_id)
            return order

    # ── Query ───────────────────────────────────

    async def get_active_orders(self, account_id: int = 0) -> list[OrderRecord]:
        """Get all non-terminal orders, optionally filtered by account.

        Args:
            account_id: Filter by account ID (0 = all accounts).

        Returns:
            List of active OrderRecord objects.
        """
        async with self._lock:
            active = [
                o for o in self._orders.values()
                if not o.is_terminal
                and (account_id == 0 or o.account_id == account_id)
            ]
            return sorted(active, key=lambda o: o.created_at, reverse=True)

    async def get_orders_by_symbol(self, symbol: str, limit: int = 50) -> list[OrderRecord]:
        """Get recent orders for a symbol.

        Args:
            symbol: Trading symbol.
            limit: Max number of orders to return.

        Returns:
            List of OrderRecord objects sorted by creation time descending.
        """
        async with self._lock:
            filtered = [o for o in self._orders.values() if o.symbol == symbol]
            return sorted(filtered, key=lambda o: o.created_at, reverse=True)[:limit]

    async def get_order_count(self) -> dict[str, int]:
        """Get count of orders by state.

        Returns:
            Dict mapping state name to count.
        """
        async with self._lock:
            counts: dict[str, int] = {}
            for order in self._orders.values():
                state_name = order.state.value
                counts[state_name] = counts.get(state_name, 0) + 1
            return counts

    # ── Cleanup ─────────────────────────────────

    async def _cleanup_loop(self) -> None:
        """Background task to remove stale terminal orders."""
        logger.info("OrderManager cleanup loop started")
        try:
            while self._running:
                await asyncio.sleep(self._cleanup_interval)
                await self._cleanup_stale()
        except asyncio.CancelledError:
            logger.info("OrderManager cleanup loop stopped")

    async def _cleanup_stale(self) -> int:
        """Remove terminal orders older than cache_ttl.

        Returns:
            Number of orders removed.
        """
        async with self._lock:
            stale_ids = [
                cid for cid, order in self._orders.items()
                if order.is_terminal and order.age_seconds > self._cache_ttl
            ]
            for cid in stale_ids:
                order = self._orders.pop(cid)
                if order.mt5_ticket:
                    self._ticket_index.pop(order.mt5_ticket, None)

            if stale_ids:
                logger.debug("OrderManager cleaned %d stale orders", len(stale_ids))
            return len(stale_ids)

    # ── Health ──────────────────────────────────

    async def health_check(self) -> dict:
        """Check order manager health.

        Returns:
            Dict with cache stats and status.
        """
        async with self._lock:
            total = len(self._orders)
            active = sum(1 for o in self._orders.values() if not o.is_terminal)
            counts = {}
            for order in self._orders.values():
                state_name = order.state.value
                counts[state_name] = counts.get(state_name, 0) + 1

        return {
            "status": "healthy",
            "total_orders": total,
            "active_orders": active,
            "by_state": counts,
        }
