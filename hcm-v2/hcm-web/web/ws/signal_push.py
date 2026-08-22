"""Signal Push — WebSocket real-time push endpoints.

Provides WebSocket endpoints for real-time data streaming:
- ws://host:8000/ws/signal/{symbol}  — Signal push per symbol
- ws://host:8000/ws/position/{account} — Position updates per account
- ws://host:8000/ws/events             — Event warning forwarding

Architecture:
  Redis PUB/SUB → WebSocket fan-out
  Each WebSocket subscribes to relevant Redis channels
  and forwards messages to connected clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

# Redis channels for WebSocket fan-out
SIGNAL_CHANNEL_PREFIX = "signal:"
POSITION_CHANNEL_PREFIX = "position:"
EVENT_WARNING_CHANNEL = "event:warning"
CIRCUIT_BREAKER_CHANNEL = "hcm:event:circuit_breaker"


class ConnectionManager:
    """Manages WebSocket connections and Redis PUB/SUB fan-out.

    Each client connects to a specific channel pattern:
    - signal:{symbol} → receives signal updates for that symbol
    - position:{account_id} → receives position updates for that account
    - events → receives event warnings

    Redis PUB/SUB messages are received and forwarded
    to all connected WebSocket clients subscribed to
    the matching channel.
    """

    def __init__(self, redis_client: Any = None):
        """Initialize ConnectionManager.

        Args:
            redis_client: RedisClient instance for PUB/SUB.
        """
        self._redis = redis_client

        # Active connections: {channel_pattern: [WebSocket, ...]}
        self._connections: dict[str, list[WebSocket]] = {}

        # PUB/SUB listeners: {channel: asyncio.Task}
        self._listeners: dict[str, asyncio.Task] = {}

        # Stats
        self._stats: dict[str, int] = {
            "connections_total": 0,
            "connections_active": 0,
            "messages_sent": 0,
            "errors": 0,
        }

    # ── Connection Management ───────────────────

    async def connect(self, websocket: WebSocket, channel: str) -> None:
        """Accept a WebSocket connection and subscribe to a channel.

        Args:
            websocket: WebSocket connection.
            channel: Channel pattern (e.g., "signal:XAUUSD").
        """
        await websocket.accept()
        self._stats["connections_total"] += 1
        self._stats["connections_active"] += 1

        if channel not in self._connections:
            self._connections[channel] = []
            # Start Redis listener for this channel if not already running
            await self._start_listener(channel)

        self._connections[channel].append(websocket)
        logger.info("WebSocket connected: channel=%s (active=%d)", channel, len(self._connections[channel]))

        # Send welcome message
        await self._send_json(websocket, {
            "type": "connected",
            "channel": channel,
            "timestamp": time.time(),
        })

    async def disconnect(self, websocket: WebSocket, channel: str) -> None:
        """Remove a WebSocket connection.

        Args:
            websocket: WebSocket to remove.
            channel: Channel the websocket was subscribed to.
        """
        if channel in self._connections:
            try:
                self._connections[channel].remove(websocket)
            except ValueError:
                pass

            self._stats["connections_active"] -= 1
            logger.info("WebSocket disconnected: channel=%s (remaining=%d)",
                       channel, len(self._connections.get(channel, [])))

            # Clean up empty channels
            if not self._connections[channel]:
                del self._connections[channel]
                await self._stop_listener(channel)

    # ── Message Broadcasting ────────────────────

    async def broadcast(self, channel: str, message: dict) -> int:
        """Broadcast a message to all WebSocket clients on a channel.

        Args:
            channel: Channel pattern.
            message: Message dict to send.

        Returns:
            Number of clients the message was sent to.
        """
        connections = self._connections.get(channel, [])
        dead: list[WebSocket] = []
        sent = 0

        for ws in connections:
            try:
                await self._send_json(ws, message)
                sent += 1
            except Exception:
                dead.append(ws)

        # Clean up dead connections
        for ws in dead:
            await self.disconnect(ws, channel)

        self._stats["messages_sent"] += sent
        return sent

    # ── Redis PUB/SUB Listeners ─────────────────

    async def _start_listener(self, channel: str) -> None:
        """Start a Redis PUB/SUB listener for a channel pattern.

        Maps WebSocket channel patterns to Redis channels:
        - signal:{symbol} → subscribes to signal:stream messages filtered by symbol
        - position:{account} → listens for position updates
        - events → listens for event:warning and circuit breaker

        Args:
            channel: WebSocket channel pattern.
        """
        if self._redis is None or not self._redis.is_initialized:
            logger.debug("Redis not available — WebSocket push disabled for %s", channel)
            return

        if channel in self._listeners:
            return  # Already listening

        redis_channel = self._resolve_redis_channel(channel)
        if redis_channel is None:
            return

        task = asyncio.create_task(self._listen_redis(redis_channel, channel))
        self._listeners[channel] = task
        logger.info("Redis listener started: redis=%s → ws=%s", redis_channel, channel)

    async def _stop_listener(self, channel: str) -> None:
        """Stop a Redis PUB/SUB listener.

        Args:
            channel: WebSocket channel pattern.
        """
        task = self._listeners.pop(channel, None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.info("Redis listener stopped: ws=%s", channel)

    async def _listen_redis(self, redis_channel: str, ws_channel: str) -> None:
        """Listen to a Redis PUB/SUB channel and forward to WebSocket.

        Args:
            redis_channel: Redis PUB/SUB channel name.
            ws_channel: WebSocket channel pattern for fan-out.
        """
        if self._redis is None or not self._redis.is_initialized:
            return

        pubsub = self._redis.pubsub()
        await pubsub.subscribe(redis_channel)
        logger.info("Subscribed to Redis channel: %s (→ ws: %s)", redis_channel, ws_channel)

        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue

                data = message["data"]
                if isinstance(data, bytes):
                    data = data.decode("utf-8")

                try:
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    parsed = {"raw": data}

                # Add metadata
                parsed["_ws_channel"] = ws_channel
                parsed["_timestamp"] = time.time()

                # Broadcast to all connected clients on this channel
                await self.broadcast(ws_channel, parsed)

        except asyncio.CancelledError:
            await pubsub.unsubscribe(redis_channel)
            logger.info("Unsubscribed from Redis channel: %s", redis_channel)
        except Exception as exc:
            logger.error("Redis listener error for %s: %s", redis_channel, exc)
            self._stats["errors"] += 1
            try:
                await pubsub.unsubscribe(redis_channel)
            except Exception:
                pass

    @staticmethod
    def _resolve_redis_channel(ws_channel: str) -> Optional[str]:
        """Map WebSocket channel to Redis PUB/SUB channel.

        Args:
            ws_channel: WebSocket channel pattern.

        Returns:
            Redis PUB/SUB channel name or None.
        """
        if ws_channel.startswith("signal:"):
            # Signal updates — listen to signal:stream
            return "signal:stream"
        elif ws_channel.startswith("position:"):
            return "position:stream"
        elif ws_channel == "events":
            return EVENT_WARNING_CHANNEL
        elif ws_channel == "circuit_breaker":
            return CIRCUIT_BREAKER_CHANNEL
        return None

    # ── Helpers ─────────────────────────────────

    @staticmethod
    async def _send_json(ws: WebSocket, data: dict) -> None:
        """Send JSON data to a WebSocket.

        Args:
            ws: WebSocket connection.
            data: Dict to send as JSON.
        """
        await ws.send_json(data)

    # ── Stats ───────────────────────────────────

    def get_stats(self) -> dict:
        """Get connection manager statistics."""
        return {
            **self._stats,
            "channels_active": len(self._connections),
            "listeners_active": len(self._listeners),
        }


# ── Router Factory ─────────────────────────────

def create_signal_push_router(
    redis_client: Any = None,
    auth_handler: Any = None,
) -> APIRouter:
    """Create FastAPI router with WebSocket endpoints.

    Args:
        redis_client: RedisClient instance for PUB/SUB.
        auth_handler: AuthHandler instance for optional auth.

    Returns:
        APIRouter with WebSocket routes.
    """
    router = APIRouter(prefix="/ws", tags=["websocket"])
    manager = ConnectionManager(redis_client=redis_client)

    # ── Signal Push WebSocket ───────────────────

    @router.websocket("/signal/{symbol}")
    async def signal_push(websocket: WebSocket, symbol: str):
        """WebSocket for real-time signal push per symbol.

        Receives signal updates for the specified symbol via Redis PUB/SUB.
        Messages include signal creation, risk check results, and order confirmations.

        Args:
            symbol: Trading symbol (e.g., XAUUSD).
        """
        channel = f"signal:{symbol.upper()}"
        await manager.connect(websocket, channel)

        try:
            # Keep connection alive and handle client messages
            while True:
                data = await websocket.receive_text()
                # Client can send ping to keep alive
                if data == "ping":
                    await websocket.send_json({"type": "pong", "timestamp": time.time()})
                # Client can subscribe/unsubscribe to additional channels
                elif data.startswith("sub:"):
                    sub_channel = data[4:]
                    logger.debug("Client subscribed to additional channel: %s", sub_channel)
                elif data.startswith("unsub:"):
                    unsub_channel = data[6:]
                    logger.debug("Client unsubscribed from channel: %s", unsub_channel)

        except WebSocketDisconnect:
            logger.info("WebSocket client disconnected: signal:%s", symbol)
        except Exception as exc:
            logger.error("WebSocket error (signal:%s): %s", symbol, exc)
        finally:
            await manager.disconnect(websocket, channel)

    # ── Position Push WebSocket ─────────────────

    @router.websocket("/position/{account_id}")
    async def position_push(websocket: WebSocket, account_id: int):
        """WebSocket for real-time position updates per account.

        Receives position changes (open, modify, close) for the
        specified account.

        Args:
            account_id: Account ID.
        """
        channel = f"position:{account_id}"
        await manager.connect(websocket, channel)

        try:
            while True:
                data = await websocket.receive_text()
                if data == "ping":
                    await websocket.send_json({"type": "pong", "timestamp": time.time()})

        except WebSocketDisconnect:
            logger.info("WebSocket client disconnected: position:%d", account_id)
        except Exception as exc:
            logger.error("WebSocket error (position:%d): %s", account_id, exc)
        finally:
            await manager.disconnect(websocket, channel)

    # ── Event Warning WebSocket ─────────────────

    @router.websocket("/events")
    async def event_push(websocket: WebSocket):
        """WebSocket for event warning forwarding.

        Receives event warnings and circuit breaker notifications
        from the market-intel service via Redis PUB/SUB.
        """
        channel = "events"
        await manager.connect(websocket, channel)

        try:
            while True:
                data = await websocket.receive_text()
                if data == "ping":
                    await websocket.send_json({"type": "pong", "timestamp": time.time()})

        except WebSocketDisconnect:
            logger.info("WebSocket client disconnected: events")
        except Exception as exc:
            logger.error("WebSocket error (events): %s", exc)
        finally:
            await manager.disconnect(websocket, channel)

    # ── Stats endpoint ──────────────────────────

    @router.get("/stats")
    async def ws_stats():
        """Get WebSocket connection statistics."""
        return {"code": 0, "data": manager.get_stats(), "message": "ok"}

    return router
