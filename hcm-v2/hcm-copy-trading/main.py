"""hcm-copy-trading — 跟单引擎（延迟优化核心，目标 <100ms）

XREADGROUP 消费 signal:risk_passed → 内存品种映射(哈希O(1)) →
手数计算(本地缓存) → Gateway gRPC 下单(<50ms) → ACK

事件驱动消费链路:
  1. XGROUP CREATE copy-trading-group on signal:risk_passed (idempotent)
  2. XREADGROUP 消费风控通过信号，先恢复 pending 消息
  3. 去重检查 (Redis SET NX)
  4. SymbolMapper 内存哈希 O(1) 品种映射 (支持 exact/prefix/suffix/regex)
  5. LotCalculator 五种手数模式 (FIXED/MULTIPLIER/RISK_PERCENT/BALANCE_RATIO/EQUITY_RATIO)
  6. OrderExecutor gRPC 直连 Gateway 下单 (主路径 <50ms)
  7. gRPC 失败 → Redis PUB 信号副本 (fallback)
  8. 成功 → ACK
  9. 重试 3 次失败 → signal:dead 死信队列

性能目标: 端到端延迟 <100ms (内存哈希 + 本地缓存 + gRPC)
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # Docker flat layout
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))  # dev project root
# Cross-service import: GatewayClient from dispatcher
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hcm-dispatcher"))

from fastapi import FastAPI

from shared.health import HealthChecker, create_health_router
from shared.logging_setup import setup_logging
from shared.redis_client import RedisClient
from shared.db import DatabasePool
from shared.config_provider import ConfigProviderV3

from copy_trading.stream_consumer import CopyTradingStreamConsumer, CopyConsumerConfig
from copy_trading.symbol_mapper import SymbolMapper
from copy_trading.lot_calculator import LotCalculator
from copy_trading.order_executor import OrderExecutor

# Also import GatewayClient for direct gRPC
from dispatcher.gateway_client import GatewayClient

# ── Configuration ──────────────────────────────

SERVICE_NAME = "hcm-copy-trading"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8009"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DB_URL = os.getenv("DB_URL", "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2")
GATEWAY_HOST = os.getenv("GATEWAY_HOST", "hcm-gateway")
GATEWAY_PORT = int(os.getenv("GATEWAY_PORT", "8004"))
CONSUMER_NAME = os.getenv("COPY_CONSUMER_NAME", "copy-consumer-1")

# ── Logging ────────────────────────────────────

log = setup_logging(service_name=SERVICE_NAME, level="INFO")

# ── Dependencies ───────────────────────────────

health = HealthChecker(service_name=SERVICE_NAME, version="2.0.0")
redis_client = RedisClient(url=REDIS_URL)
db_pool = DatabasePool(dsn=DB_URL)
config_provider: ConfigProviderV3 = None  # initialized in startup

# Copy trading components
gateway_client: GatewayClient = None
symbol_mapper: SymbolMapper = None
lot_calculator: LotCalculator = None
order_executor: OrderExecutor = None
stream_consumer: CopyTradingStreamConsumer = None


# ── Health Checks ──────────────────────────────

async def check_redis():
    return await redis_client.health_check()

async def check_pg():
    return await db_pool.health_check()

async def check_gateway():
    if gateway_client:
        return await gateway_client.health_check()
    return {"status": "not_connected"}

async def check_mapper():
    if symbol_mapper:
        return await symbol_mapper.health_check()
    return {"status": "not_initialized"}

async def check_executor():
    if order_executor:
        return await order_executor.health_check()
    return {"status": "not_initialized"}

async def check_consumer():
    if stream_consumer:
        return await stream_consumer.health_check()
    return {"status": "not_started"}

health.add_check("redis", check_redis)
health.add_check("postgresql", check_pg)
health.add_check("gateway", check_gateway)
health.add_check("symbol_mapper", check_mapper)
health.add_check("order_executor", check_executor)
health.add_check("copy_consumer", check_consumer)


# ── FastAPI ────────────────────────────────────

app = FastAPI(
    title="HCM Copy Trading",
    description="Copy trading engine — <100ms latency target with in-memory mapping",
    version="2.0.0",
)
app.include_router(create_health_router(health))


@app.on_event("startup")
async def startup():
    global config_provider, gateway_client, symbol_mapper
    global lot_calculator, order_executor, stream_consumer

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

    # Initialize config provider
    config_provider = ConfigProviderV3(
        pg_pool=db_pool,
        redis_client=redis_client.raw if redis_client.is_initialized else None,
    )
    await config_provider.initialize()
    log.info("ConfigProviderV3 initialized")

    # Initialize Gateway client
    gateway_client = GatewayClient(
        host=GATEWAY_HOST,
        port=GATEWAY_PORT,
    )
    await gateway_client.connect()

    # Initialize SymbolMapper
    symbol_mapper = SymbolMapper(
        db_pool=db_pool,
        redis_client=redis_client,
    )
    await symbol_mapper.initialize()

    # Initialize LotCalculator
    lot_calculator = LotCalculator(
        db_pool=db_pool,
        config_provider=config_provider,
    )
    await lot_calculator.load_config()

    # Initialize OrderExecutor
    order_executor = OrderExecutor(
        gateway_client=gateway_client,
        redis_client=redis_client,
    )

    # Initialize stream consumer
    consumer_config = CopyConsumerConfig(consumer_name=CONSUMER_NAME)
    stream_consumer = CopyTradingStreamConsumer(
        redis_client=redis_client,
        symbol_mapper=symbol_mapper,
        lot_calculator=lot_calculator,
        order_executor=order_executor,
        config=consumer_config,
        db_pool=db_pool,
    )
    await stream_consumer.start()

    log.info("%s fully initialized", SERVICE_NAME)


@app.on_event("shutdown")
async def shutdown():
    log.info("Shutting down %s", SERVICE_NAME)

    if stream_consumer:
        await stream_consumer.stop()
    if symbol_mapper:
        await symbol_mapper.shutdown()
    if gateway_client:
        await gateway_client.close()
    if config_provider:
        await config_provider.shutdown()
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
            "mappings": "/mappings",
        },
    }


@app.get("/stats")
async def stats():
    """Get copy trading statistics."""
    return {
        "service": SERVICE_NAME,
        "consumer": stream_consumer.get_stats() if stream_consumer else {},
        "mapper": symbol_mapper.get_stats() if symbol_mapper else {},
        "executor": order_executor.get_stats() if order_executor else {},
    }


@app.get("/mappings")
async def mappings():
    """Get current symbol mappings."""
    if symbol_mapper:
        return symbol_mapper.get_stats()
    return {"mappings": 0}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT, log_level="info")
