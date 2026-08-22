"""hcm-gateway — MT5 桥接服务（gRPC + WebSocket）

服务入口：FastAPI + gRPC Server 双协议。
- gRPC: 接收 Dispatcher/Copy-Trading 下单请求，转发到 MT5
- REST: POST /api/v1/place_order — HTTP 下单（Dispatcher 主通道）
- WebSocket: 实时报价推送到 Dashboard
- /health: 健康检查端点
"""

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional, Any

# Ensure shared library is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # Docker flat layout
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))  # dev project root

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from shared.health import HealthChecker, create_health_router
from shared.logging_setup import setup_logging
from shared.redis_client import RedisClient

# ── Configuration ──────────────────────────────

SERVICE_NAME = "hcm-gateway"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8004"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

# ── Logging ────────────────────────────────────

log = setup_logging(service_name=SERVICE_NAME, level="INFO")

# ── Health ─────────────────────────────────────

health = HealthChecker(service_name=SERVICE_NAME, version="2.0.0")
redis_client = RedisClient(url=REDIS_URL)


async def check_redis():
    """Check Redis connectivity."""
    return await redis_client.health_check()


health.add_check("redis", check_redis)


# ── FastAPI Application ────────────────────────

app = FastAPI(
    title="HCM Gateway",
    description="MT5 Bridge — gRPC + WebSocket",
    version="2.0.0",
)

app.include_router(create_health_router(health))


# ── gRPC Server instance (lazy init) ──────────

_grpc_server: Any = None  # GatewayGrpcServer


def get_grpc_server() -> Any:
    """Get or create the GatewayGrpcServer instance."""
    global _grpc_server
    if _grpc_server is None:
        from gateway.grpc_server import GatewayGrpcServer
        _grpc_server = GatewayGrpcServer(
            mt5_bridge=None,
            order_manager=None,
            redis_client=redis_client,
        )
    return _grpc_server


# ── HTTP Order API ─────────────────────────────

class PlaceOrderHttpRequest(BaseModel):
    """HTTP request body matching PlaceOrderRequest fields."""
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


@app.post("/api/v1/place_order")
async def http_place_order(req: PlaceOrderHttpRequest):
    """HTTP REST endpoint for order placement.

    Delegates to GatewayGrpcServer.place_order() — the same logic
    used by the gRPC path. This is the primary channel for the
    hcm-dispatcher when gRPC stubs are unavailable.

    Returns:
        JSON {code, message, mt5_ticket, filled_price, commission, latency_ms}
    """
    from gateway.grpc_server import PlaceOrderRequest

    grpc_req = PlaceOrderRequest(
        client_id=req.client_id,
        account_id=req.account_id,
        symbol=req.symbol,
        direction=req.direction,
        lot=req.lot,
        sl=req.sl,
        tp=req.tp,
        magic=req.magic,
        comment=req.comment,
        order_type=req.order_type,
        entry_price=req.entry_price,
        slippage=req.slippage,
    )

    server = get_grpc_server()
    response = await server.place_order(grpc_req)
    return {
        "code": response.code,
        "message": response.message,
        "mt5_ticket": response.mt5_ticket,
        "filled_price": response.filled_price,
        "commission": response.commission,
        "latency_ms": response.latency_ms,
    }


# ── WebSocket Endpoint ────────────────────────

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time signal streaming.

    Subscribes to Redis PUB/SUB channels:
      - signal:stream — raw signals from Dispatcher
      - signal:risk_passed — signals cleared by Risk Engine

    Pushes each published message to the connected client as JSON text.
    """
    await websocket.accept()
    pubsub = redis_client.pubsub()
    await pubsub.subscribe("signal:stream", "signal:risk_passed")
    log.info("WS client connected, subscribed to signal:stream + signal:risk_passed")
    try:
        while True:
            message = await pubsub.get_message(timeout=1.0)
            if message and message.get("type") == "message":
                await websocket.send_text(message["data"])
            await asyncio.sleep(0.05)
    except WebSocketDisconnect:
        log.info("WS client disconnected")
    except Exception as exc:
        log.warning("WS error: %s", exc)
    finally:
        await pubsub.unsubscribe("signal:stream", "signal:risk_passed")
        await pubsub.close()


@app.on_event("startup")
async def startup():
    """Initialize connections on service startup."""
    log.info("Starting %s on port %d", SERVICE_NAME, SERVICE_PORT)
    try:
        await redis_client.initialize()
        log.info("Redis connected")
    except Exception as exc:
        log.warning("Redis not available: %s (continuing without Redis)", exc)

    # Initialize gRPC server (warm-up the singleton)
    get_grpc_server()
    log.info("GatewayGrpcServer initialized for HTTP order API")


@app.on_event("shutdown")
async def shutdown():
    """Graceful shutdown."""
    log.info("Shutting down %s", SERVICE_NAME)
    if _grpc_server is not None:
        await _grpc_server.stop()
    await redis_client.shutdown()


@app.get("/")
async def root():
    """Service root endpoint."""
    return {
        "service": SERVICE_NAME,
        "version": "2.0.0",
        "status": "running",
        "endpoints": {
            "grpc": f"0.0.0.0:{SERVICE_PORT}",
            "ws": f"0.0.0.0:{os.getenv('WS_PORT', '8005')}",
            "health": "/health",
            "metrics": "/metrics",
        },
    }


# ── Main ───────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT, log_level="info")
