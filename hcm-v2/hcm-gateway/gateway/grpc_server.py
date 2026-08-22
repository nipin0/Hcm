"""gRPC Server — implements the Gateway gRPC service from gateway.proto.

Provides the server-side implementation for:
- PlaceOrder (unary)
- CancelOrder (unary)
- StreamPrices (server-streaming)
- GetAccountInfo (unary)
- HealthCheck (unary)

In production, this uses the generated gRPC stubs from gateway.proto.
For development, a minimal stub-free implementation is provided that
mirrors the proto contract.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Optional

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────

DEFAULT_GRPC_PORT = 8004
DEFAULT_DEADLINE_SEC = 30
PRICE_STREAM_INTERVAL = 1.0  # seconds between price push


# ── Message Classes (mirroring proto definitions) ──

@dataclass
class PlaceOrderRequest:
    """Mirror of gateway.proto PlaceOrderRequest."""
    client_id: str = ""
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


@dataclass
class PlaceOrderResponse:
    """Mirror of gateway.proto PlaceOrderResponse."""
    code: int = 0
    message: str = "ok"
    mt5_ticket: int = 0
    filled_price: float = 0.0
    commission: float = 0.0
    latency_ms: int = 0


@dataclass
class CancelOrderRequest:
    """Mirror of gateway.proto CancelOrderRequest."""
    account_id: int = 0
    mt5_ticket: int = 0
    client_id: str = ""


@dataclass
class CancelOrderResponse:
    """Mirror of gateway.proto CancelOrderResponse."""
    code: int = 0
    message: str = "ok"
    mt5_ticket: int = 0


@dataclass
class PriceStreamRequest:
    """Mirror of gateway.proto PriceStreamRequest."""
    account_id: str = ""
    symbols: list[str] = field(default_factory=list)
    include_ticks: bool = False


@dataclass
class PriceTickMsg:
    """Mirror of gateway.proto PriceTick."""
    symbol: str = ""
    bid: float = 0.0
    ask: float = 0.0
    spread: float = 0.0
    timestamp: int = 0
    volume: int = 0


@dataclass
class AccountRequest:
    """Mirror of gateway.proto AccountRequest."""
    account_id: int = 0


@dataclass
class AccountInfoMsg:
    """Mirror of gateway.proto AccountInfo."""
    code: int = 0
    message: str = "ok"
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
class HealthCheckRequest:
    """Mirror of gateway.proto HealthCheckRequest."""
    pass


@dataclass
class HealthCheckResponse:
    """Mirror of gateway.proto HealthCheckResponse."""
    status: str = "healthy"
    uptime_sec: int = 0
    mt5_connected: bool = False
    redis_connected: bool = False
    version: str = "2.0.0"


class GatewayGrpcServer:
    """gRPC server implementation for the Gateway service.

    Handles incoming gRPC requests and delegates to the MT5 bridge
    and order manager.

    Example:
        server = GatewayGrpcServer(mt5_bridge, order_manager, redis_client)
        await server.start(port=8004)
    """

    def __init__(
        self,
        mt5_bridge: Any = None,
        order_manager: Any = None,
        redis_client: Any = None,
        ws_server: Any = None,
    ):
        """Initialize gRPC server.

        Args:
            mt5_bridge: Mt5Bridge instance for MT5 connectivity.
            order_manager: OrderManager instance for order tracking.
            redis_client: RedisClient instance for health checks.
            ws_server: WsPriceServer instance for price broadcasting.
        """
        self._mt5 = mt5_bridge
        self._order_mgr = order_manager
        self._redis = redis_client
        self._ws = ws_server
        self._start_time: float = 0.0
        self._running = False
        self._grpc_server: Any = None  # grpc.aio.Server when available

    # ── Lifecycle ───────────────────────────────

    async def start(self, port: int = DEFAULT_GRPC_PORT) -> None:
        """Start the gRPC server.

        Args:
            port: Port number to listen on.
        """
        self._start_time = time.time()
        self._running = True

        try:
            import grpc
            from concurrent import futures

            self._grpc_server = grpc.aio.server(
                futures.ThreadPoolExecutor(max_workers=10),
                options=[
                    ('grpc.max_receive_message_length', 10 * 1024 * 1024),
                    ('grpc.max_send_message_length', 10 * 1024 * 1024),
                ],
            )

            # In production, the generated stubs would be added here:
            #   from proto import gateway_pb2_grpc
            #   gateway_pb2_grpc.add_GatewayServicer_to_server(self, self._grpc_server)

            self._grpc_server.add_insecure_port(f"0.0.0.0:{port}")
            await self._grpc_server.start()
            logger.info("gRPC server started on port %d", port)

        except ImportError:
            logger.warning(
                "grpc package not available — gRPC server operating in stub mode. "
                "Install: pip install grpcio grpcio-tools"
            )
            # In stub mode, we expose the same methods for direct Python calls
            logger.info("gRPC stub mode: methods available for direct invocation on port %d", port)

    async def stop(self) -> None:
        """Gracefully stop the gRPC server."""
        self._running = False
        if self._grpc_server is not None:
            await self._grpc_server.stop(grace=5.0)
            logger.info("gRPC server stopped")

    # ── RPC: PlaceOrder ─────────────────────────

    async def place_order(self, request: PlaceOrderRequest) -> PlaceOrderResponse:
        """Handle PlaceOrder RPC.

        Creates an order record, executes via MT5 bridge, and tracks the result.

        Args:
            request: PlaceOrderRequest with order parameters.

        Returns:
            PlaceOrderResponse with ticket and fill details.
        """
        t0 = time.time()

        try:
            # Create order record in PENDING state
            if self._order_mgr is not None:
                order = await self._order_mgr.create_order(
                    client_id=request.client_id or f"auto-{int(t0*1000000)}",
                    account_id=request.account_id,
                    symbol=request.symbol,
                    direction=request.direction,
                    lot=request.lot,
                    sl=request.sl,
                    tp=request.tp,
                    magic=request.magic,
                    comment=request.comment,
                    order_type=request.order_type,
                    entry_price=request.entry_price,
                    slippage=request.slippage,
                )

                # Execute order via MT5 bridge
                if self._mt5 is not None:
                    result = await self._mt5.place_order(
                        symbol=request.symbol,
                        direction=request.direction,
                        lot=request.lot,
                        sl=request.sl,
                        tp=request.tp,
                        magic=request.magic,
                        comment=request.comment,
                        order_type=request.order_type,
                        entry_price=request.entry_price,
                        slippage=request.slippage,
                    )

                    if result.success:
                        from gateway.order_manager import OrderState
                        await self._order_mgr.update_state(
                            client_id=order.client_id,
                            new_state=OrderState.PLACED if result.ticket else OrderState.REJECTED,
                            mt5_ticket=result.ticket,
                            filled_price=result.filled_price,
                            commission=result.commission,
                            latency_ms=result.latency_ms,
                        )

                        latency_ms = int((time.time() - t0) * 1000)
                        return PlaceOrderResponse(
                            code=0,
                            message="Order placed successfully",
                            mt5_ticket=result.ticket,
                            filled_price=result.filled_price,
                            commission=result.commission,
                            latency_ms=latency_ms,
                        )
                    else:
                        from gateway.order_manager import OrderState
                        await self._order_mgr.update_state(
                            client_id=order.client_id,
                            new_state=OrderState.REJECTED,
                            error_message=result.error_message,
                            error_code=result.error_code,
                        )
                        return PlaceOrderResponse(
                            code=result.error_code or 1,
                            message=result.error_message,
                            latency_ms=int(result.latency_ms),
                        )
            else:
                # No order manager — execute directly
                if self._mt5 is not None:
                    result = await self._mt5.place_order(
                        symbol=request.symbol,
                        direction=request.direction,
                        lot=request.lot,
                        sl=request.sl,
                        tp=request.tp,
                        magic=request.magic,
                        comment=request.comment,
                        order_type=request.order_type,
                        entry_price=request.entry_price,
                        slippage=request.slippage,
                    )
                    latency_ms = int((time.time() - t0) * 1000)
                    return PlaceOrderResponse(
                        code=0 if result.success else 1,
                        message="ok" if result.success else result.error_message,
                        mt5_ticket=result.ticket,
                        filled_price=result.filled_price,
                        commission=result.commission,
                        latency_ms=latency_ms,
                    )

        except ValueError as exc:
            logger.warning("PlaceOrder validation error: %s", exc)
            return PlaceOrderResponse(code=1, message=str(exc))
        except Exception as exc:
            logger.error("PlaceOrder error: %s", exc)
            return PlaceOrderResponse(code=99, message=f"Internal error: {exc}")

        # Stub mode
        latency_ms = int((time.time() - t0) * 1000)
        stub_ticket = int(time.time() * 1000) % 1000000000
        return PlaceOrderResponse(
            code=0,
            message="ok (stub)",
            mt5_ticket=stub_ticket,
            filled_price=0.0,
            commission=0.0,
            latency_ms=latency_ms,
        )

    # ── RPC: CancelOrder ────────────────────────

    async def cancel_order(self, request: CancelOrderRequest) -> CancelOrderResponse:
        """Handle CancelOrder RPC.

        Args:
            request: CancelOrderRequest with ticket to cancel.

        Returns:
            CancelOrderResponse with result.
        """
        t0 = time.time()

        try:
            if self._order_mgr is not None and request.client_id:
                await self._order_mgr.cancel_order(request.client_id)

            if self._mt5 is not None and request.mt5_ticket:
                result = await self._mt5.cancel_order(request.mt5_ticket)
                if result.success:
                    return CancelOrderResponse(
                        code=0,
                        message="Order cancelled",
                        mt5_ticket=request.mt5_ticket,
                    )
                else:
                    return CancelOrderResponse(
                        code=1,
                        message=result.error_message,
                        mt5_ticket=request.mt5_ticket,
                    )

        except Exception as exc:
            logger.error("CancelOrder error: %s", exc)
            return CancelOrderResponse(code=99, message=str(exc))

        return CancelOrderResponse(code=0, message="ok (stub)", mt5_ticket=request.mt5_ticket)

    # ── RPC: StreamPrices ───────────────────────

    async def stream_prices(
        self, request: PriceStreamRequest
    ) -> AsyncIterator[PriceTickMsg]:
        """Handle StreamPrices RPC (server-streaming).

        Continuously streams price ticks for subscribed symbols.

        Args:
            request: PriceStreamRequest with symbol list.

        Yields:
            PriceTickMsg for each price update.
        """
        symbols = request.symbols if request.symbols else ["XAUUSD"]
        logger.info("Price stream started for symbols=%s", symbols)

        try:
            while self._running:
                for symbol in symbols:
                    if self._mt5 is not None:
                        tick = await self._mt5.get_current_price(symbol)
                    else:
                        import random
                        base = 4100.0 if symbol.startswith("XAU") else 65000.0
                        tick = type('Tick', (), {
                            'symbol': symbol,
                            'bid': base + random.uniform(-5, 5),
                            'ask': base + random.uniform(-5, 5) + 0.5,
                            'spread': 0.5,
                            'timestamp': time.time(),
                            'volume': 100,
                        })()

                    if tick is not None:
                        yield PriceTickMsg(
                            symbol=tick.symbol,
                            bid=tick.bid,
                            ask=tick.ask,
                            spread=tick.spread,
                            timestamp=int(tick.timestamp * 1000),
                            volume=tick.volume if hasattr(tick, 'volume') else 0,
                        )

                await asyncio.sleep(PRICE_STREAM_INTERVAL)

        except asyncio.CancelledError:
            logger.info("Price stream cancelled for symbols=%s", symbols)

    # ── RPC: GetAccountInfo ─────────────────────

    async def get_account_info(self, request: AccountRequest) -> AccountInfoMsg:
        """Handle GetAccountInfo RPC.

        Args:
            request: AccountRequest with account_id.

        Returns:
            AccountInfoMsg with account details.
        """
        try:
            if self._mt5 is not None:
                summary = await self._mt5.get_account_info()
                return AccountInfoMsg(
                    code=0,
                    message="ok",
                    account_id=summary.account_id,
                    account_name=summary.account_name,
                    balance=summary.balance,
                    equity=summary.equity,
                    margin=summary.margin,
                    free_margin=summary.free_margin,
                    margin_level=summary.margin_level,
                    leverage=summary.leverage,
                    currency=summary.currency,
                    server_time=summary.server_time,
                    is_connected=summary.is_connected,
                )
        except Exception as exc:
            logger.error("GetAccountInfo error: %s", exc)
            return AccountInfoMsg(code=1, message=str(exc))

        return AccountInfoMsg(
            code=0,
            message="ok (stub)",
            account_id=request.account_id,
            account_name="Stub Account",
            balance=10000.0,
            equity=10000.0,
            margin=0.0,
            free_margin=10000.0,
            leverage=100,
            currency="USD",
            server_time=int(time.time() * 1000),
            is_connected=True,
        )

    # ── RPC: HealthCheck ────────────────────────

    async def health_check(self, request: HealthCheckRequest) -> HealthCheckResponse:
        """Handle HealthCheck RPC.

        Args:
            request: HealthCheckRequest (empty).

        Returns:
            HealthCheckResponse with service status.
        """
        mt5_ok = await self._mt5.is_connected() if self._mt5 else False
        redis_ok = False
        if self._redis is not None:
            try:
                redis_ok = await self._redis.ping()
            except Exception:
                pass

        uptime = int(time.time() - self._start_time) if self._start_time > 0 else 0

        status = "healthy"
        if not mt5_ok:
            status = "degraded"

        return HealthCheckResponse(
            status=status,
            uptime_sec=uptime,
            mt5_connected=mt5_ok,
            redis_connected=redis_ok,
            version="2.0.0",
        )

    # ── Properties ──────────────────────────────

    @property
    def is_running(self) -> bool:
        """Whether the gRPC server is running."""
        return self._running

    @property
    def uptime_seconds(self) -> float:
        """Server uptime in seconds."""
        if self._start_time == 0.0:
            return 0.0
        return time.time() - self._start_time
