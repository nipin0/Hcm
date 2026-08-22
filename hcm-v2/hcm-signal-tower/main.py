"""hcm-signal-tower — 信号塔核心服务

bar_close 触发 → 技术指标评测 → 五级市况判定 → 预评分 →
权重方案 → AI 研判(或 bypass) → 信号发布(Redis Stream + PG)
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
from shared.config_provider import ConfigProviderV3

from signal_tower.scheduler import Scheduler
from signal_tower.signal_publisher import SignalPublisher
from signal_tower.watchdog import WatchdogManager

# ── Configuration ──────────────────────────────

SERVICE_NAME = "hcm-signal-tower"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8002"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DB_URL = os.getenv("DB_URL", "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2")

# ── Logging ────────────────────────────────────

log = setup_logging(service_name=SERVICE_NAME, level="INFO")

# ── Dependencies ───────────────────────────────

health = HealthChecker(service_name=SERVICE_NAME, version="2.0.0")
redis_client = RedisClient(url=REDIS_URL)
db_pool = DatabasePool(dsn=DB_URL)
config_provider: ConfigProviderV3 = None  # initialized in startup
scheduler: Scheduler = None


async def check_redis():
    return await redis_client.health_check()


async def check_pg():
    return await db_pool.health_check()


health.add_check("redis", check_redis)
health.add_check("postgresql", check_pg)


# ── FastAPI ────────────────────────────────────

app = FastAPI(
    title="HCM Signal Tower",
    description="Signal production engine — five-regime model + weighted scoring",
    version="2.0.0",
)

app.include_router(create_health_router(health))


@app.on_event("startup")
async def startup():
    global config_provider, scheduler
    log.info("Starting %s on port %d", SERVICE_NAME, SERVICE_PORT)
    try:
        await redis_client.initialize()
    except Exception as exc:
        log.warning("Redis not available: %s", exc)
    try:
        await db_pool.initialize()
    except Exception as exc:
        log.warning("PostgreSQL not available: %s", exc)

    config_provider = ConfigProviderV3(pg_pool=db_pool, redis_client=redis_client.raw if redis_client.is_initialized else None)
    await config_provider.initialize()
    log.info("ConfigProviderV3 initialized")

    # ── Start Signal Production Scheduler ───────
    signal_publisher = SignalPublisher(redis_client=redis_client, db_pool=db_pool)
    watchdog = WatchdogManager(redis_client=redis_client, config_provider=config_provider)

    scheduler = Scheduler(
        db_pool=db_pool,
        redis_client=redis_client,
        config_provider=config_provider,
        signal_publisher=signal_publisher,
        watchdog=watchdog,
    )
    await scheduler.load_config()
    asyncio.create_task(scheduler.start())
    log.info("Signal production scheduler started")

    health.add_check("scheduler", lambda: scheduler.health_check())


@app.on_event("shutdown")
async def shutdown():
    global scheduler
    log.info("Shutting down %s", SERVICE_NAME)
    if scheduler:
        await scheduler.stop()
    if config_provider:
        await config_provider.shutdown()
    await redis_client.shutdown()
    await db_pool.shutdown()


@app.get("/")
async def root():
    return {"service": SERVICE_NAME, "version": "2.0.0", "status": "running"}


@app.get("/scheduler/stats")
async def scheduler_stats():
    """Get scheduler statistics."""
    if scheduler is None:
        return {"status": "not_started"}
    return await scheduler.health_check()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT, log_level="info")
