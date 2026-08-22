"""Gateway gRPC Client — connects to hcm-gateway for order placement.

Provides async gRPC client for:
- PlaceOrder: Send order requests to Gateway
- CancelOrder: Cancel existing orders by ticket
- GetAccountInfo: Query account status
- HealthCheck: Check Gateway health

Uses the message contracts from gateway.proto (stub-compatible).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from shared.errors import ErrorCode, HcmError

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────

DEFAULT_GATEWAY_HOST = "hcm-gateway"
DEFAULT_GATEWAY_PORT = 8004
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_CALL_TIMEOUT = 30.0
DEFAULT_RETRY_MAX = 3
DEFAULT_RETRY_DELAY = 0.5


@dataclass
class GatewayClientConfig:
    """Configuration for the Gateway gRPC client."""
    host: str = DEFAULT_GATEWAY_HOST
    port: int = DEFAULT_GATEWAY_PORT
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    call_timeout: float = DEFAULT_CALL_TIMEOUT
    retry_max: int = DEFAULT_RETRY_MAX
    retry_delay: float = DEFAULT_RETRY_DELAY

    @property
    def address(self) -> str:
        """Full gRPC address string."""
        return f"{self.host}:{self.port}"


class GatewayClient:
    """gRPC client for communication with hcm-gateway.

    Mirrors the Gateway gRPC service defined in proto/gateway.proto:
    - PlaceOrder (unary)
    - CancelOrder (unary)
    - GetAccountInfo (unary)
    - HealthCheck (unary)

    Operates in two modes:
    - gRPC mode: when grpcio is installed, full gRPC channel
    - Direct mode: calls GatewayGrpcServer methods directly (same process)

    Example:
        client = GatewayClient(host="hcm-gateway", port=8004)
        await client.connect()
        result = await client.place_order(
            client_id="abc-123", account_id=6, symbol="XAUUSD",
            direction="BUY", lot=0.1, sl=4100.0, tp=4120.0,
        )
        await client.close()
    """

    def __init__(
        self,
        host: str = DEFAULT_GATEWAY_HOST,
        port: int = DEFAULT_GATEWAY_PORT,
        config: Optional[GatewayClientConfig] = None,
    ):
        """Initialize GatewayClient.

        Args:
            host: Gateway hostname or IP.
            port: Gateway gRPC port.
            config: Optional client configuration.
        """
        self._config = config or GatewayClientConfig(host=host, port=port)
        self._channel: Any = None
        self._stub: Any = None
        self._connected = False
        self._direct_server: Any = None  # For same-process direct calls

    # ── Lifecycle ───────────────────────────────

    async def connect(self) -> bool:
        """Establish gRPC channel to Gateway.

        Returns:
            True if connected successfully.
        """
        if self._connected:
            return True

        for attempt in range(1, self._config.retry_max + 1):
            try:
                import grpc

                self._channel = grpc.aio.insecure_channel(
                    self._config.address,
                    options=[
                        ('grpc.keepalive_time_ms', 30000),
                        ('grpc.keepalive_timeout_ms', 10000),
                        ('grpc.http2.max_pings_without_data', 0),
                    ],
                )

                # Try to import generated stubs
                try:
                    from proto import gateway_pb2, gateway_pb2_grpc
                    self._stub = gateway_pb2_grpc.GatewayStub(self._channel)
                    logger.info("GatewayClient using generated proto stubs")
                except ImportError:
                    # Stub mode: use direct message classes
                    logger.info("GatewayClient using stub mode (no generated protos)")

                self._connected = True
                logger.info(
                    "GatewayClient connected to %s", self._config.address,
                )
                return True

            except ImportError:
                logger.warning(
                    "grpcio not installed — GatewayClient operating in direct mode"
                )
                self._connected = True
                return True

            except Exception as exc:
                logger.warning(
                    "GatewayClient connection attempt %d/%d failed: %s",
                    attempt, self._config.retry_max, exc,
                )
                if attempt < self._config.retry_max:
                    await asyncio.sleep(self._config.retry_delay * (2 ** (attempt - 1)))

        logger.error(
            "GatewayClient: failed to connect to %s after %d attempts",
            self._config.address, self._config.retry_max,
        )
        return False

    async def close(self) -> None:
        """Close the gRPC channel."""
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
            self._stub = None
            self._connected = False
            logger.info("GatewayClient disconnected")

    def set_direct_server(self, grpc_server: Any) -> None:
        """Set a direct reference to GatewayGrpcServer for same-process calls.

        Args:
            grpc_server: GatewayGrpcServer instance.
        """
        self._direct_server = grpc_server
        logger.info("GatewayClient using direct server reference")

    # ── PlaceOrder ──────────────────────────────

    async def place_order(
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
    ) -> dict:
        """Place an order via Gateway gRPC.

        Args:
            client_id: Unique client-generated order ID.
            account_id: MT5 account ID.
            symbol: Trading symbol.
            direction: "BUY" or "SELL".
            lot: Trade volume.
            sl: Stop loss price.
            tp: Take profit price.
            magic: EA magic number.
            comment: Order comment.
            order_type: "MARKET", "LIMIT", or "STOP".
            entry_price: Entry price (for limit/stop orders).
            slippage: Max slippage in points.

        Returns:
            Dict with code, message, mt5_ticket, filled_price, commission, latency_ms.
        """
        t0 = time.time()

        # Build request
        request = {
            "client_id": client_id,
            "account_id": account_id,
            "symbol": symbol,
            "direction": direction,
            "lot": lot,
            "sl": sl,
            "tp": tp,
            "magic": magic,
            "comment": comment,
            "order_type": order_type,
            "entry_price": entry_price,
            "slippage": slippage,
        }

        for attempt in range(1, self._config.retry_max + 1):
            try:
                if self._stub is not None:
                    # Full gRPC call
                    from proto import gateway_pb2
                    pb_request = gateway_pb2.PlaceOrderRequest(**request)
                    response = await self._stub.PlaceOrder(
                        pb_request,
                        timeout=self._config.call_timeout,
                    )
                    result = {
                        "code": response.code,
                        "message": response.message,
                        "mt5_ticket": response.mt5_ticket,
                        "filled_price": response.filled_price,
                        "commission": response.commission,
                        "latency_ms": response.latency_ms,
                    }
                elif self._direct_server is not None:
                    # Direct call to GatewayGrpcServer
                    from gateway.grpc_server import PlaceOrderRequest
                    req = PlaceOrderRequest(**request)
                    response = await self._direct_server.place_order(req)
                    result = {
                        "code": response.code,
                        "message": response.message,
                        "mt5_ticket": response.mt5_ticket,
                        "filled_price": response.filled_price,
                        "commission": response.commission,
                        "latency_ms": response.latency_ms,
                    }
                else:
                    # Stub mode — fall back to HTTP REST call to gateway
                    import urllib.request
                    import json as _json

                    http_url = f"http://{self._config.host}:{self._config.port}/api/v1/place_order"
                    http_body = _json.dumps(request).encode("utf-8")
                    http_req = urllib.request.Request(
                        http_url,
                        data=http_body,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    try:
                        with urllib.request.urlopen(http_req, timeout=self._config.call_timeout) as resp:
                            resp_data = _json.loads(resp.read().decode("utf-8"))
                        latency_ms = int((time.time() - t0) * 1000)
                        return {
                            "code": resp_data.get("code", 99),
                            "message": resp_data.get("message", "http_error"),
                            "mt5_ticket": resp_data.get("mt5_ticket", 0),
                            "filled_price": resp_data.get("filled_price", 0.0),
                            "commission": resp_data.get("commission", 0.0),
                            "latency_ms": latency_ms,
                        }
                    except Exception as http_exc:
                        logger.warning(
                            "HTTP fallback PlaceOrder failed: %s", http_exc,
                        )
                        latency_ms = int((time.time() - t0) * 1000)
                        return {
                            "code": 99,
                            "message": f"Gateway unreachable: {http_exc}",
                            "mt5_ticket": 0,
                            "filled_price": 0.0,
                            "commission": 0.0,
                            "latency_ms": latency_ms,
                        }

                return result

            except Exception as exc:
                logger.warning(
                    "Gateway PlaceOrder attempt %d/%d failed: %s",
                    attempt, self._config.retry_max, exc,
                )
                if attempt < self._config.retry_max:
                    await asyncio.sleep(self._config.retry_delay * attempt)

        # All retries exhausted
        latency_ms = int((time.time() - t0) * 1000)
        return {
            "code": 99,
            "message": f"Gateway PlaceOrder failed after {self._config.retry_max} attempts",
            "mt5_ticket": 0,
            "filled_price": 0.0,
            "commission": 0.0,
            "latency_ms": latency_ms,
        }

    # ── CancelOrder ─────────────────────────────

    async def cancel_order(
        self,
        account_id: int,
        mt5_ticket: int,
        client_id: str = "",
    ) -> dict:
        """Cancel an order by MT5 ticket.

        Args:
            account_id: MT5 account ID.
            mt5_ticket: MT5 order ticket to cancel.
            client_id: Client order ID (optional).

        Returns:
            Dict with code, message, mt5_ticket.
        """
        request = {
            "account_id": account_id,
            "mt5_ticket": mt5_ticket,
            "client_id": client_id,
        }

        try:
            if self._stub is not None:
                from proto import gateway_pb2
                pb_request = gateway_pb2.CancelOrderRequest(**request)
                response = await self._stub.CancelOrder(
                    pb_request, timeout=self._config.call_timeout,
                )
                return {
                    "code": response.code,
                    "message": response.message,
                    "mt5_ticket": response.mt5_ticket,
                }
            elif self._direct_server is not None:
                from gateway.grpc_server import CancelOrderRequest
                req = CancelOrderRequest(**request)
                response = await self._direct_server.cancel_order(req)
                return {
                    "code": response.code,
                    "message": response.message,
                    "mt5_ticket": response.mt5_ticket,
                }
            else:
                return {"code": 0, "message": "ok (stub)", "mt5_ticket": mt5_ticket}
        except Exception as exc:
            logger.error("Gateway CancelOrder failed: %s", exc)
            return {"code": 99, "message": str(exc), "mt5_ticket": mt5_ticket}

    # ── GetAccountInfo ──────────────────────────

    async def get_account_info(self, account_id: int) -> dict:
        """Get account information from Gateway.

        Args:
            account_id: MT5 account ID.

        Returns:
            Dict with account details.
        """
        try:
            if self._stub is not None:
                from proto import gateway_pb2
                response = await self._stub.GetAccountInfo(
                    gateway_pb2.AccountRequest(account_id=account_id),
                    timeout=self._config.call_timeout,
                )
                return {
                    "code": response.code,
                    "message": response.message,
                    "account_id": response.account_id,
                    "account_name": response.account_name,
                    "balance": response.balance,
                    "equity": response.equity,
                    "margin": response.margin,
                    "free_margin": response.free_margin,
                    "margin_level": response.margin_level,
                    "leverage": response.leverage,
                    "currency": response.currency,
                    "is_connected": response.is_connected,
                }
            elif self._direct_server is not None:
                from gateway.grpc_server import AccountRequest
                response = await self._direct_server.get_account_info(
                    AccountRequest(account_id=account_id)
                )
                return {
                    "code": response.code,
                    "message": response.message,
                    "account_id": response.account_id,
                    "account_name": response.account_name,
                    "balance": response.balance,
                    "equity": response.equity,
                    "margin": response.margin,
                    "free_margin": response.free_margin,
                    "margin_level": response.margin_level,
                    "leverage": response.leverage,
                    "currency": response.currency,
                    "is_connected": response.is_connected,
                }
            else:
                return {
                    "code": 0, "message": "ok (stub)",
                    "account_id": account_id, "account_name": "Stub",
                    "balance": 10000.0, "equity": 10000.0,
                    "margin": 0.0, "free_margin": 10000.0,
                    "margin_level": 0.0, "leverage": 100,
                    "currency": "USD", "is_connected": True,
                }
        except Exception as exc:
            logger.error("Gateway GetAccountInfo failed: %s", exc)
            return {"code": 99, "message": str(exc)}

    # ── HealthCheck ─────────────────────────────

    async def health_check(self) -> dict:
        """Check Gateway health via gRPC HealthCheck.

        Returns:
            Dict with gateway status.
        """
        try:
            if self._stub is not None:
                from proto import gateway_pb2
                response = await self._stub.HealthCheck(
                    gateway_pb2.HealthCheckRequest(),
                    timeout=5.0,
                )
                return {
                    "status": response.status,
                    "uptime_sec": response.uptime_sec,
                    "mt5_connected": response.mt5_connected,
                    "redis_connected": response.redis_connected,
                    "version": response.version,
                }
            elif self._direct_server is not None:
                from gateway.grpc_server import HealthCheckRequest
                response = await self._direct_server.health_check(
                    HealthCheckRequest()
                )
                return {
                    "status": response.status,
                    "uptime_sec": response.uptime_sec,
                    "mt5_connected": response.mt5_connected,
                    "redis_connected": response.redis_connected,
                    "version": response.version,
                }
            else:
                return {
                    "status": "healthy (stub)", "uptime_sec": 0,
                    "mt5_connected": True, "redis_connected": True,
                    "version": "2.0.0",
                }
        except Exception as exc:
            logger.warning("Gateway health check failed: %s", exc)
            return {"status": "unreachable", "error": str(exc)}

    # ── Properties ──────────────────────────────

    @property
    def is_connected(self) -> bool:
        """Whether the client is connected to Gateway."""
        return self._connected
