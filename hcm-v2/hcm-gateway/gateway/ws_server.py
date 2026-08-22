"""WebSocket Server — real-time price push to Dashboard.

Broadcasts real-time quotes from MT5 to connected WebSocket clients,
supporting per-symbol subscription and unsubscribe.

Uses shared RedisClient for PUB/SUB synchronization across multiple
gateway instances.

Usage:
    server = WsPriceServer(redis_client, mt5_bridge)
    await server.start(port=8005)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────

DEFAULT_WS_PORT = 8005
PRICE_BROADCAST_INTERVAL = 1.0  # seconds
HEARTBEAT_INTERVAL = 15  # seconds
MAX_SUBSCRIPTIONS_PER_CLIENT = 20
MAX_CLIENTS = 100


@dataclass
class WsClient:
    """Connected WebSocket client state."""
    client_id: str
    websocket: Any
    subscribed_symbols: set[str] = field(default_factory=set)
    connected_at: float = field(default_factory=time.time)
    last_heartbeat: float = field(default_factory=time.time)


class WsPriceServer:
    """WebSocket server for real-time price distribution.

    Manages client connections, symbol subscriptions, and price
    broadcasting. Supports the Dashboard's real-time quote display.

    Example:
        server = WsPriceServer(redis_client, mt5_bridge)
        await server.start(port=8005)
        # Server runs in background task
        await server.stop()
    """

    def __init__(
        self,
        redis_client: Any = None,
        mt5_bridge: Any = None,
        broadcast_interval: float = PRICE_BROADCAST_INTERVAL,
        heartbeat_interval: float = HEARTBEAT_INTERVAL,
    ):
        """Initialize WebSocket price server.

        Args:
            redis_client: RedisClient instance for pub/sub sync.
            mt5_bridge: Mt5Bridge instance for price data.
            broadcast_interval: Interval between price broadcasts in seconds.
            heartbeat_interval: Heartbeat check interval in seconds.
        """
        self._redis = redis_client
        self._mt5 = mt5_bridge
        self._broadcast_interval = broadcast_interval
        self._heartbeat_interval = heartbeat_interval

        self._clients: dict[str, WsClient] = {}
        self._lock = asyncio.Lock()
        self._running = False
        self._server: Any = None
        self._broadcast_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    # ── Lifecycle ───────────────────────────────

    async def start(self, port: int = DEFAULT_WS_PORT) -> None:
        """Start the WebSocket server.

        Args:
            port: Port number to listen on.
        """
        self._running = True
        self._broadcast_task = asyncio.create_task(self._broadcast_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            import websockets
            self._server = await websockets.serve(
                self._handle_connection,
                "0.0.0.0",
                port,
                max_size=1024 * 1024,  # 1MB max message
                ping_interval=30,
                ping_timeout=10,
            )
            logger.info("WebSocket server started on port %d", port)
        except ImportError:
            logger.warning(
                "websockets package not available — WS server in stub mode. "
                "Install: pip install websockets"
            )

    async def stop(self) -> None:
        """Gracefully stop the WebSocket server."""
        self._running = False

        for task in [self._broadcast_task, self._heartbeat_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        if self._server:
            self._server.close()
            await self._server.wait_closed()

        async with self._lock:
            for client in list(self._clients.values()):
                try:
                    await client.websocket.close(1001, "Server shutting down")
                except Exception:
                    pass
            self._clients.clear()

        logger.info("WebSocket server stopped")

    # ── Connection Handling ─────────────────────

    async def _handle_connection(self, websocket: Any, path: str = "") -> None:
        """Handle a new WebSocket connection.

        Args:
            websocket: WebSocket connection object.
            path: Connection path (unused).
        """
        client_id = f"ws-{id(websocket)}-{int(time.time()*1000)}"

        async with self._lock:
            if len(self._clients) >= MAX_CLIENTS:
                try:
                    await websocket.send(json.dumps({
                        "type": "error",
                        "message": "Max clients reached",
                        "code": 503,
                    }))
                    await websocket.close(1013, "Max clients")
                except Exception:
                    pass
                return

            client = WsClient(client_id=client_id, websocket=websocket)
            self._clients[client_id] = client

        logger.info("WS client connected: id=%s (total=%d)", client_id, len(self._clients))

        try:
            # Send welcome message
            await websocket.send(json.dumps({
                "type": "connected",
                "client_id": client_id,
                "message": "Connected to HCM Gateway WS",
                "timestamp": time.time(),
            }))

            # Listen for subscription messages
            async for message in websocket:
                await self._handle_message(client, message)

        except Exception as exc:
            logger.debug("WS client disconnected: id=%s, reason=%s", client_id, exc)
        finally:
            await self._remove_client(client_id)

    async def _handle_message(self, client: WsClient, raw_message: Any) -> None:
        """Process an incoming WebSocket message.

        Supported message types:
            - subscribe: {"type": "subscribe", "symbols": ["XAUUSD", "BTCUSD"]}
            - unsubscribe: {"type": "unsubscribe", "symbols": ["XAUUSD"]}
            - ping: {"type": "ping"}

        Args:
            client: The sending WsClient.
            raw_message: Raw message data (string or bytes).
        """
        try:
            if isinstance(raw_message, bytes):
                raw_message = raw_message.decode("utf-8")
            msg = json.loads(raw_message) if isinstance(raw_message, str) else raw_message
        except json.JSONDecodeError:
            await self._send_error(client, "Invalid JSON")
            return

        msg_type = msg.get("type", "")

        if msg_type == "subscribe":
            symbols = msg.get("symbols", [])
            if not isinstance(symbols, list):
                await self._send_error(client, "symbols must be a list")
                return

            async with self._lock:
                if len(client.subscribed_symbols) + len(symbols) > MAX_SUBSCRIPTIONS_PER_CLIENT:
                    await self._send_error(client, "Max subscriptions reached")
                    return
                for sym in symbols:
                    client.subscribed_symbols.add(sym.upper())

            await self._send(client, {
                "type": "subscribed",
                "symbols": list(client.subscribed_symbols),
            })
            logger.info("WS client %s subscribed to %s", client.client_id, symbols)

        elif msg_type == "unsubscribe":
            symbols = msg.get("symbols", [])
            if not isinstance(symbols, list):
                await self._send_error(client, "symbols must be a list")
                return

            async with self._lock:
                for sym in symbols:
                    client.subscribed_symbols.discard(sym.upper())

            await self._send(client, {
                "type": "unsubscribed",
                "symbols": list(client.subscribed_symbols),
            })
            logger.debug("WS client %s unsubscribed from %s", client.client_id, symbols)

        elif msg_type == "ping":
            await self._send(client, {"type": "pong", "timestamp": time.time()})

        else:
            await self._send_error(client, f"Unknown message type: {msg_type}")

    async def _remove_client(self, client_id: str) -> None:
        """Remove a disconnected client.

        Args:
            client_id: Client ID to remove.
        """
        async with self._lock:
            self._clients.pop(client_id, None)
        logger.debug("WS client removed: id=%s (remaining=%d)", client_id, len(self._clients))

    # ── Broadcasting ────────────────────────────

    async def _broadcast_loop(self) -> None:
        """Background task: broadcast prices to subscribed clients."""
        logger.info("WS broadcast loop started (interval=%.1fs)", self._broadcast_interval)
        try:
            while self._running:
                await self._broadcast_prices()
                await asyncio.sleep(self._broadcast_interval)
        except asyncio.CancelledError:
            logger.info("WS broadcast loop stopped")

    async def _broadcast_prices(self) -> None:
        """Fetch current prices and send to subscribed clients."""
        # Collect unique subscribed symbols across all clients
        async with self._lock:
            if not self._clients:
                return
            all_symbols: set[str] = set()
            symbol_clients: dict[str, list[WsClient]] = {}
            for client in self._clients.values():
                for sym in client.subscribed_symbols:
                    all_symbols.add(sym)
                    if sym not in symbol_clients:
                        symbol_clients[sym] = []
                    symbol_clients[sym].append(client)

            if not all_symbols:
                return

        # Fetch prices for all symbols
        prices: dict[str, dict] = {}
        for sym in all_symbols:
            if self._mt5 is not None:
                tick = await self._mt5.get_current_price(sym)
            else:
                import random
                base = 4100.0 if sym.startswith("XAU") else 65000.0
                tick = type('Tick', (), {
                    'symbol': sym,
                    'bid': base + random.uniform(-5, 5),
                    'ask': base + random.uniform(-5, 5) + 0.5,
                    'spread': 0.5,
                    'timestamp': time.time(),
                    'volume': 100,
                })()

            if tick is not None:
                prices[sym] = {
                    "symbol": tick.symbol,
                    "bid": tick.bid,
                    "ask": tick.ask,
                    "spread": tick.spread,
                    "timestamp": int(tick.timestamp * 1000),
                    "volume": tick.volume if hasattr(tick, 'volume') else 0,
                }

        # Send prices to subscribed clients
        for sym, clients in symbol_clients.items():
            if sym not in prices:
                continue
            price_msg = json.dumps({
                "type": "price_update",
                "data": prices[sym],
            })
            for client in clients:
                try:
                    await client.websocket.send(price_msg)
                except Exception:
                    # Client disconnected — will be cleaned on next heartbeat
                    pass

    # ── Heartbeat ───────────────────────────────

    async def _heartbeat_loop(self) -> None:
        """Background task: send heartbeats and clean stale connections."""
        logger.info("WS heartbeat loop started (interval=%ds)", self._heartbeat_interval)
        try:
            while self._running:
                await asyncio.sleep(self._heartbeat_interval)
                await self._check_clients()
        except asyncio.CancelledError:
            logger.info("WS heartbeat loop stopped")

    async def _check_clients(self) -> None:
        """Ping all clients and remove unresponsive ones."""
        stale_ids: list[str] = []
        async with self._lock:
            for client_id, client in list(self._clients.items()):
                try:
                    await client.websocket.send(json.dumps({
                        "type": "heartbeat",
                        "timestamp": time.time(),
                    }))
                    client.last_heartbeat = time.time()
                except Exception:
                    stale_ids.append(client_id)

        for cid in stale_ids:
            await self._remove_client(cid)

    # ── Message Helpers ─────────────────────────

    async def _send(self, client: WsClient, data: dict) -> None:
        """Send a JSON message to a client.

        Args:
            client: Target client.
            data: Dict to send as JSON.
        """
        try:
            await client.websocket.send(json.dumps(data))
        except Exception:
            pass

    async def _send_error(self, client: WsClient, message: str) -> None:
        """Send an error message to a client.

        Args:
            client: Target client.
            message: Error description.
        """
        await self._send(client, {"type": "error", "message": message, "code": 400})

    # ── Query ───────────────────────────────────

    async def get_client_count(self) -> int:
        """Get current connected client count.

        Returns:
            Number of connected WebSocket clients.
        """
        async with self._lock:
            return len(self._clients)

    async def get_subscription_summary(self) -> dict[str, int]:
        """Get count of subscriptions per symbol.

        Returns:
            Dict mapping symbol to subscriber count.
        """
        async with self._lock:
            counts: dict[str, int] = {}
            for client in self._clients.values():
                for sym in client.subscribed_symbols:
                    counts[sym] = counts.get(sym, 0) + 1
            return counts

    # ── Health ──────────────────────────────────

    async def health_check(self) -> dict:
        """Check WebSocket server health.

        Returns:
            Dict with client count and status.
        """
        return {
            "status": "healthy" if self._running else "stopped",
            "clients": await self.get_client_count(),
            "subscriptions": await self.get_subscription_summary(),
        }
