"""MT5 TCP Bridge — direct connection to MetaTrader 5 Terminal.

Provides MT5 connection management, authentication, heartbeat monitoring,
and order execution through the MT5 Python API.

This module is designed as a reservation for MT5 Terminal connectivity.
In production, MetaTrader5 Python package must be installed and MT5 Terminal
must be running on the same machine.

Usage:
    bridge = Mt5Bridge(account_id=12345, password="***", server="ICMarkets-Demo")
    await bridge.connect()
    connected = await bridge.is_connected()
    await bridge.disconnect()
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

DEFAULT_HEARTBEAT_INTERVAL = 30  # seconds
DEFAULT_RECONNECT_MAX = 5
DEFAULT_RECONNECT_DELAY = 2.0   # seconds
MT5_TIMEOUT = 60  # seconds


class Mt5ConnectionState(str, Enum):
    """MT5 Terminal connection states."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    AUTHENTICATED = "authenticated"
    RECONNECTING = "reconnecting"
    ERROR = "error"


@dataclass
class Mt5OrderResult:
    """Result of an MT5 order operation."""
    success: bool = False
    ticket: int = 0
    filled_price: float = 0.0
    commission: float = 0.0
    error_code: int = 0
    error_message: str = ""
    latency_ms: float = 0.0


@dataclass
class Mt5AccountSummary:
    """MT5 account information."""
    account_id: int = 0
    account_name: str = ""
    balance: float = 0.0
    equity: float = 0.0
    margin: float = 0.0
    free_margin: float = 0.0
    margin_level: float = 0.0
    leverage: int = 100
    currency: str = "USD"
    server_time: int = 0
    is_connected: bool = False


@dataclass
class PriceTick:
    """Real-time price tick from MT5."""
    symbol: str = ""
    bid: float = 0.0
    ask: float = 0.0
    spread: float = 0.0
    timestamp: float = field(default_factory=time.time)
    volume: int = 0


class Mt5Bridge:
    """TCP bridge to MetaTrader 5 Terminal.

    Handles connection lifecycle, heartbeat monitoring, order execution,
    and account information retrieval via the MetaTrader5 Python API.

    In the current phase, MT5 integration is stubbed for development.
    When MT5 Terminal is available, the MetaTrader5 package must be
    installed and the _mt5 attribute will be populated.

    Example:
        bridge = Mt5Bridge(
            account_id=12345,
            password=os.getenv("MT5_PASSWORD", ""),
            server=os.getenv("MT5_SERVER", "ICMarkets-Demo"),
        )
        await bridge.connect()
    """

    def __init__(
        self,
        account_id: int = 0,
        password: str = "",
        server: str = "",
        heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL,
        reconnect_max: int = DEFAULT_RECONNECT_MAX,
        reconnect_delay: float = DEFAULT_RECONNECT_DELAY,
    ):
        """Initialize MT5 Bridge.

        Args:
            account_id: MT5 account ID (login).
            password: MT5 account password.
            server: MT5 server name.
            heartbeat_interval: Heartbeat check interval in seconds.
            reconnect_max: Maximum reconnection attempts.
            reconnect_delay: Initial reconnection delay (doubles each attempt).
        """
        self._account_id = account_id
        self._password = password
        self._server = server
        self._heartbeat_interval = heartbeat_interval
        self._reconnect_max = reconnect_max
        self._reconnect_delay = reconnect_delay

        self._state: Mt5ConnectionState = Mt5ConnectionState.DISCONNECTED
        self._mt5 = None  # Reserved for MetaTrader5 import
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._last_heartbeat: float = 0.0
        self._connect_time: float = 0.0
        self._error_count: int = 0
        self._last_error: str = ""

        # Try to import MetaTrader5 if available
        try:
            import MetaTrader5 as _mt5_lib
            self._mt5 = _mt5_lib
            logger.info("MetaTrader5 Python package loaded")
        except ImportError:
            logger.info(
                "MetaTrader5 package not installed — MT5 bridge operates in stub mode. "
                "Install with: pip install MetaTrader5"
            )

    # ── Connection Lifecycle ────────────────────

    async def connect(self) -> bool:
        """Establish connection to MT5 Terminal.

        Returns:
            True if connected successfully.
        """
        self._state = Mt5ConnectionState.CONNECTING
        logger.info(
            "MT5 Bridge connecting: account=%d, server=%s",
            self._account_id, self._server or "N/A",
        )

        if self._mt5 is not None:
            try:
                initialized = self._mt5.initialize(
                    login=self._account_id,
                    password=self._password,
                    server=self._server,
                    timeout=MT5_TIMEOUT,
                )
                if not initialized:
                    error = self._mt5.last_error()
                    self._state = Mt5ConnectionState.ERROR
                    self._last_error = f"MT5 init failed: {error}"
                    logger.error(self._last_error)
                    return False

                self._state = Mt5ConnectionState.CONNECTED
                self._connect_time = time.time()
                self._last_heartbeat = time.time()

                # Start heartbeat monitor
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                logger.info("MT5 Bridge connected successfully")
                return True

            except Exception as exc:
                self._state = Mt5ConnectionState.ERROR
                self._last_error = str(exc)
                logger.error("MT5 Bridge connection error: %s", exc)
                return False

        # Stub mode: simulate connected for development
        logger.info("MT5 Bridge: stub mode — simulating connected state")
        self._state = Mt5ConnectionState.CONNECTED
        self._connect_time = time.time()
        self._last_heartbeat = time.time()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        return True

    async def disconnect(self) -> None:
        """Gracefully disconnect from MT5 Terminal."""
        logger.info("MT5 Bridge disconnecting...")
        self._state = Mt5ConnectionState.DISCONNECTED

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._mt5 is not None:
            try:
                self._mt5.shutdown()
            except Exception as exc:
                logger.warning("MT5 shutdown error: %s", exc)

        logger.info("MT5 Bridge disconnected")

    async def is_connected(self) -> bool:
        """Check if MT5 connection is active.

        Returns:
            True if connected and heartbeat is recent.
        """
        if self._state not in (
            Mt5ConnectionState.CONNECTED,
            Mt5ConnectionState.AUTHENTICATED,
        ):
            return False

        # Check heartbeat freshness
        if time.time() - self._last_heartbeat > self._heartbeat_interval * 2:
            logger.warning("MT5 Bridge heartbeat stale")
            return False

        if self._mt5 is not None:
            try:
                return self._mt5.terminal_info() is not None
            except Exception:
                return False

        return True  # Stub mode

    # ── Heartbeat ───────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Background heartbeat monitoring loop."""
        logger.info("MT5 Bridge heartbeat started (interval=%ds)", self._heartbeat_interval)
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                try:
                    alive = await self.is_connected()
                    self._last_heartbeat = time.time()
                    if not alive:
                        logger.warning("MT5 Bridge heartbeat check failed — triggering reconnect")
                        await self._reconnect()
                except Exception as exc:
                    logger.error("MT5 Bridge heartbeat error: %s", exc)
        except asyncio.CancelledError:
            logger.info("MT5 Bridge heartbeat stopped")

    async def _reconnect(self) -> None:
        """Attempt reconnection with exponential backoff."""
        self._state = Mt5ConnectionState.RECONNECTING
        for attempt in range(1, self._reconnect_max + 1):
            delay = self._reconnect_delay * (2 ** (attempt - 1))
            logger.info("MT5 Bridge reconnect attempt %d/%d (delay=%.1fs)", attempt, self._reconnect_max, delay)
            await asyncio.sleep(delay)

            if await self.connect():
                logger.info("MT5 Bridge reconnected successfully")
                return

        self._state = Mt5ConnectionState.ERROR
        self._error_count += 1
        logger.error("MT5 Bridge: all %d reconnect attempts failed", self._reconnect_max)

    # ── Order Execution ─────────────────────────

    async def place_order(
        self,
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
    ) -> Mt5OrderResult:
        """Place an order via MT5 Terminal.

        Args:
            symbol: Trading symbol (e.g., "XAUUSD").
            direction: "BUY" or "SELL".
            lot: Trade volume in lots.
            sl: Stop loss price (0 = none).
            tp: Take profit price (0 = none).
            magic: EA magic number.
            comment: Order comment.
            order_type: "MARKET", "LIMIT", or "STOP".
            entry_price: Entry price (required for LIMIT/STOP).
            slippage: Maximum slippage in points.

        Returns:
            Mt5OrderResult with ticket number and fill details.
        """
        t0 = time.time()

        if direction.upper() not in ("BUY", "SELL"):
            return Mt5OrderResult(
                success=False,
                error_message=f"Invalid direction: {direction}",
            )

        if self._mt5 is not None:
            try:
                # Build MT5 order request
                mt5_direction = (
                    self._mt5.ORDER_TYPE_BUY if direction.upper() == "BUY"
                    else self._mt5.ORDER_TYPE_SELL
                )

                request = {
                    "action": self._mt5.TRADE_ACTION_DEAL,
                    "symbol": symbol,
                    "volume": lot,
                    "type": mt5_direction,
                    "sl": sl,
                    "tp": tp,
                    "deviation": slippage,
                    "magic": magic,
                    "comment": comment,
                    "type_time": self._mt5.ORDER_TIME_GTC,
                    "type_filling": self._mt5.ORDER_FILLING_IOC,
                }

                if order_type.upper() == "LIMIT":
                    request["type"] = (
                        self._mt5.ORDER_TYPE_BUY_LIMIT if direction.upper() == "BUY"
                        else self._mt5.ORDER_TYPE_SELL_LIMIT
                    )
                    request["price"] = entry_price
                elif order_type.upper() == "STOP":
                    request["type"] = (
                        self._mt5.ORDER_TYPE_BUY_STOP if direction.upper() == "BUY"
                        else self._mt5.ORDER_TYPE_SELL_STOP
                    )
                    request["price"] = entry_price

                result = self._mt5.order_send(request)
                latency = (time.time() - t0) * 1000

                if result is None or result.retcode != self._mt5.TRADE_RETCODE_DONE:
                    error_code = result.retcode if result else -1
                    error_msg = result.comment if result else "No response from MT5"
                    logger.error(
                        "MT5 order failed: symbol=%s, direction=%s, lot=%s, error=%s",
                        symbol, direction, lot, error_msg,
                    )
                    return Mt5OrderResult(
                        success=False,
                        error_code=error_code,
                        error_message=error_msg,
                        latency_ms=latency,
                    )

                logger.info(
                    "MT5 order placed: ticket=%d, symbol=%s, direction=%s, "
                    "lot=%s, filled=%.5f, latency=%.1fms",
                    result.order, symbol, direction, lot, result.price, latency,
                )
                return Mt5OrderResult(
                    success=True,
                    ticket=result.order,
                    filled_price=result.price,
                    commission=result.commission if hasattr(result, "commission") else 0.0,
                    latency_ms=latency,
                )

            except Exception as exc:
                latency = (time.time() - t0) * 1000
                logger.error("MT5 order exception: %s", exc)
                return Mt5OrderResult(
                    success=False,
                    error_message=str(exc),
                    latency_ms=latency,
                )

        # Stub mode: simulate order placement for development
        latency = (time.time() - t0) * 1000
        stub_ticket = int(time.time() * 1000) % 1000000000
        logger.info(
            "MT5 Bridge stub mode: order placed | symbol=%s, direction=%s, "
            "lot=%s, stub_ticket=%d, latency=%.1fms",
            symbol, direction, lot, stub_ticket, latency,
        )
        return Mt5OrderResult(
            success=True,
            ticket=stub_ticket,
            filled_price=0.0,
            commission=0.0,
            latency_ms=latency,
        )

    async def cancel_order(self, ticket: int) -> Mt5OrderResult:
        """Cancel a pending order by ticket number.

        Args:
            ticket: MT5 order ticket number.

        Returns:
            Mt5OrderResult indicating success/failure.
        """
        t0 = time.time()

        if self._mt5 is not None:
            try:
                result = self._mt5.order_send({
                    "action": self._mt5.TRADE_ACTION_REMOVE,
                    "order": ticket,
                })
                latency = (time.time() - t0) * 1000

                if result is None or result.retcode != self._mt5.TRADE_RETCODE_DONE:
                    return Mt5OrderResult(
                        success=False,
                        error_message=result.comment if result else "No response",
                        latency_ms=latency,
                    )

                logger.info("MT5 order cancelled: ticket=%d", ticket)
                return Mt5OrderResult(success=True, ticket=ticket, latency_ms=latency)

            except Exception as exc:
                latency = (time.time() - t0) * 1000
                return Mt5OrderResult(success=False, error_message=str(exc), latency_ms=latency)

        # Stub mode
        latency = (time.time() - t0) * 1000
        logger.info("MT5 Bridge stub mode: order cancelled | ticket=%d", ticket)
        return Mt5OrderResult(success=True, ticket=ticket, latency_ms=latency)

    # ── Account Information ─────────────────────

    async def get_account_info(self) -> Mt5AccountSummary:
        """Retrieve MT5 account information.

        Returns:
            Mt5AccountSummary with balance, equity, margin, etc.
        """
        if self._mt5 is not None:
            try:
                info = self._mt5.account_info()
                if info is None:
                    logger.error("MT5 account_info() returned None")
                    return Mt5AccountSummary(is_connected=False)

                return Mt5AccountSummary(
                    account_id=info.login,
                    account_name=info.name or "",
                    balance=info.balance,
                    equity=info.equity,
                    margin=info.margin,
                    free_margin=info.margin_free,
                    margin_level=info.margin_level or 0.0,
                    leverage=info.leverage,
                    currency=info.currency,
                    server_time=int(time.time() * 1000),
                    is_connected=True,
                )
            except Exception as exc:
                logger.error("MT5 account_info error: %s", exc)
                return Mt5AccountSummary(is_connected=False)

        # Stub mode
        return Mt5AccountSummary(
            account_id=self._account_id,
            account_name="Stub Account",
            balance=10000.0,
            equity=10000.0,
            margin=0.0,
            free_margin=10000.0,
            margin_level=0.0,
            leverage=100,
            currency="USD",
            server_time=int(time.time() * 1000),
            is_connected=self._state == Mt5ConnectionState.CONNECTED,
        )

    # ── Price Streaming ─────────────────────────

    async def get_current_price(self, symbol: str) -> Optional[PriceTick]:
        """Get current bid/ask for a symbol.

        Args:
            symbol: Trading symbol.

        Returns:
            PriceTick or None if unavailable.
        """
        if self._mt5 is not None:
            try:
                tick = self._mt5.symbol_info_tick(symbol)
                if tick is None:
                    return None

                return PriceTick(
                    symbol=symbol,
                    bid=tick.bid,
                    ask=tick.ask,
                    spread=round((tick.ask - tick.bid) / self._mt5.symbol_info(symbol).point if self._mt5.symbol_info(symbol) else tick.ask - tick.bid, 5),
                    timestamp=time.time(),
                    volume=tick.volume if hasattr(tick, "volume") else 0,
                )
            except Exception as exc:
                logger.warning("MT5 get_current_price(%s) error: %s", symbol, exc)
                return None

        # Stub mode: generate simulated price
        import random
        base = 4100.0 if symbol.startswith("XAU") else 65000.0
        jitter = random.uniform(-5.0, 5.0)
        spread = 0.50 if symbol.startswith("XAU") else 15.0
        return PriceTick(
            symbol=symbol,
            bid=round(base + jitter, 2),
            ask=round(base + jitter + spread, 2),
            spread=spread,
            timestamp=time.time(),
            volume=100,
        )

    # ── Health ──────────────────────────────────

    async def health_check(self) -> dict:
        """Check MT5 bridge health.

        Returns:
            Dict with status, state, and connection info.
        """
        connected = await self.is_connected()
        uptime = time.time() - self._connect_time if self._connect_time > 0 else 0.0

        return {
            "status": "healthy" if connected else "unhealthy",
            "state": self._state.value,
            "mt5_connected": connected,
            "uptime_seconds": round(uptime, 1),
            "error_count": self._error_count,
            "last_error": self._last_error,
            "mt5_available": self._mt5 is not None,
        }

    # ── Properties ──────────────────────────────

    @property
    def state(self) -> Mt5ConnectionState:
        """Current connection state."""
        return self._state

    @property
    def mt5_available(self) -> bool:
        """Whether MetaTrader5 package is installed."""
        return self._mt5 is not None
