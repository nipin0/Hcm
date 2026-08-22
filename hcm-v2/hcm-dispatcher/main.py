"""hcm-dispatcher — 信号分发服务

XREADGROUP 消费 signal:risk_passed → Gateway gRPC 下单 → 订单状态跟踪。

事件驱动消费链路:
  1. XGROUP CREATE dispatcher-group on signal:risk_passed (idempotent)
  2. XREADGROUP 消费风控通过信号，先恢复 pending 消息
  3. GatewayClient gRPC 调用 PlaceOrder 下单
  4. OrderTracker 跟踪订单生命周期 (PENDING → PLACED → FILLED)
  5. 成功 → ACK
  6. 重试 3 次失败 → signal:dead 死信队列
  7. 超时检测: >30s 未成交标记 TIMEOUT
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import FastAPI

from shared.health import HealthChecker, create_health_router
from shared.logging_setup import setup_logging
from shared.redis_client import RedisClient
from shared.db import DatabasePool

from dispatcher.stream_consumer import DispatcherStreamConsumer, DispatcherConsumerConfig
from dispatcher.gateway_client import GatewayClient, GatewayClientConfig
from dispatcher.order_tracker import OrderTracker
from dispatcher.notifier import Notifier
from dispatcher.execution_notifier import ExecutionNotifier

# ── Configuration ──────────────────────────────

SERVICE_NAME = "hcm-dispatcher"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8008"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DB_URL = os.getenv("DB_URL", "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2")
GATEWAY_HOST = os.getenv("GATEWAY_HOST", "hcm-gateway")
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8004"))
CONSUMER_NAME = os.getenv("DISPATCHER_CONSUMER_NAME", "dispatcher-consumer-1")
ORDER_TIMEOUT = float(os.getenv("ORDER_TIMEOUT_SEC", "30.0"))
HEALTH_CHECK_TIMEOUT = float(os.getenv("HEALTH_CHECK_TIMEOUT_SEC", "5.0"))

# ── Logging ────────────────────────────────────

log = setup_logging(service_name=SERVICE_NAME, level="INFO")

# ── Dependencies ───────────────────────────────

health = HealthChecker(service_name=SERVICE_NAME, version="2.0.0")
redis_client = RedisClient(url=REDIS_URL)
db_pool = DatabasePool(dsn=DB_URL)

# Dispatcher components
gateway_client: GatewayClient = None
order_tracker: OrderTracker = None
stream_consumer: DispatcherStreamConsumer = None
execution_notifier: ExecutionNotifier = None


# ── Health Checks ──────────────────────────────

async def check_redis():
    try:
        return await asyncio.wait_for(
            redis_client.health_check(),
            timeout=HEALTH_CHECK_TIMEOUT,
        )
    except asyncio.TimeoutError:
        return {"status": "unhealthy", "error": f"Redis health check timed out after {HEALTH_CHECK_TIMEOUT}s"}

async def check_pg():
    return await db_pool.health_check()

async def check_gateway():
    if gateway_client:
        return await gateway_client.health_check()
    return {"status": "not_connected"}

async def check_dispatcher():
    if stream_consumer:
        return await stream_consumer.health_check()
    return {"status": "not_started"}

health.add_check("redis", check_redis)
health.add_check("postgresql", check_pg)
health.add_check("gateway", check_gateway)
health.add_check("dispatcher", check_dispatcher)


# ── FastAPI ────────────────────────────────────

app = FastAPI(
    title="HCM Dispatcher",
    description="Order dispatcher via gRPC to Gateway with lifecycle tracking",
    version="2.0.0",
)
app.include_router(create_health_router(health))


@app.on_event("startup")
async def startup():
    global gateway_client, order_tracker, stream_consumer

    log.info("Starting %s on port %d", SERVICE_NAME, SERVICE_PORT)

    # Initialize infrastructure
    try:
        await redis_client.initialize()
    except Exception as exc:
        log.warning("Redis not available: %s", exc)

    try:
        await db_pool.initialize()
    except Exception as exc:
        log.warning("PostgreSQL not available: %s", exc)

    # Initialize Gateway client
    gateway_client = GatewayClient(
        host=GATEWAY_HOST,
        port=GATEWAY_PORT,
    )
    await gateway_client.connect()

    # Initialize notifier (reads notification config from PG hcm_config.metadata)
    notifier = Notifier(db_pool=db_pool)

    # Initialize order tracker
    # 注意: notifier=None —— OrderTracker 的下单状态来自 dispatcher stub 假成功,
    # 由其驱动通知会推假 ticket / "No-trade"。真实开仓通知改由 ExecutionNotifier 消费
    # order:executed 流触发(单一事实源 = MT5 真实开仓)。
    order_tracker = OrderTracker(order_timeout=ORDER_TIMEOUT, notifier=None)
    await order_tracker.start()

    # ExecutionNotifier: 消费 MT5 真实开仓事件 → 钉钉通知
    execution_notifier = ExecutionNotifier(redis_client=redis_client, notifier=notifier)
    await execution_notifier.start()

    # Initialize stream consumer
    consumer_config = DispatcherConsumerConfig(consumer_name=CONSUMER_NAME)
    stream_consumer = DispatcherStreamConsumer(
        redis_client=redis_client,
        gateway_client=gateway_client,
        order_tracker=order_tracker,
        db_pool=db_pool,
        config=consumer_config,
    )
    await stream_consumer.start()

    log.info("%s fully initialized", SERVICE_NAME)


@app.on_event("shutdown")
async def shutdown():
    log.info("Shutting down %s", SERVICE_NAME)

    if stream_consumer:
        await stream_consumer.stop()
    if execution_notifier:
        await execution_notifier.stop()
    if order_tracker:
        await order_tracker.stop()
    if gateway_client:
        await gateway_client.close()
    await redis_client.shutdown()
    await db_pool.shutdown()


@app.get("/")
async def root():
    return {
        "service": SERVICE_NAME,
        "version": "2.0.0",
        "status": "running",
        "gateway": f"{GATEWAY_HOST}:{GATEWAY_PORT}",
        "endpoints": {
            "health": "/health",
            "metrics": "/metrics",
            "ready": "/ready",
            "live": "/live",
            "stats": "/stats",
            "orders": "/orders",
        },
    }


@app.get("/stats")
async def stats():
    """Get dispatcher statistics."""
    return {
        "service": SERVICE_NAME,
        "dispatcher": await stream_consumer.get_stats() if stream_consumer else {},
        "tracker": await order_tracker.get_stats() if order_tracker else {},
    }


@app.get("/orders")
async def active_orders():
    """Get active orders being tracked."""
    if order_tracker:
        active = await order_tracker.get_active_orders()
        return {
            "active_count": len(active),
            "orders": [
                {
                    "client_id": o.client_id,
                    "signal_id": o.signal_id,
                    "symbol": o.symbol,
                    "direction": o.direction,
                    "lot": o.lot,
                    "mt5_ticket": o.mt5_ticket,
                    "state": o.state.value,
                    "latency_ms": o.latency_ms,
                }
                for o in active
            ],
        }
    return {"active_count": 0, "orders": []}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT, log_level="info")
