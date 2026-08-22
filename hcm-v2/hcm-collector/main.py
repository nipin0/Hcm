"""hcm-collector — 行情采集服务（K线/Tick）

从 Gateway WebSocket 接收实时报价 → 采集 K线/Tick → 写入 PostgreSQL + Redis 缓存。
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
from collector.kline_collector import KlineCollector, RedisPriceSource

# ── Configuration ──────────────────────────────

SERVICE_NAME = "hcm-collector"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8001"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DB_URL = os.getenv("DB_URL", "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2")

# ── Logging ────────────────────────────────────

log = setup_logging(service_name=SERVICE_NAME, level="INFO")

# ── Dependencies ───────────────────────────────

health = HealthChecker(service_name=SERVICE_NAME, version="2.0.0")
redis_client = RedisClient(url=REDIS_URL)
db_pool = DatabasePool(dsn=DB_URL)
config_provider = ConfigProviderV3()
kline_collector: KlineCollector | None = None


async def check_redis():
    return await redis_client.health_check()


async def check_pg():
    return await db_pool.health_check()


health.add_check("redis", check_redis)
health.add_check("postgresql", check_pg)


# ── FastAPI ────────────────────────────────────

app = FastAPI(
    title="HCM Collector",
    description="K-line & Tick data collector",
    version="2.0.0",
)

app.include_router(create_health_router(health))


@app.on_event("startup")
async def startup():
    log.info("Starting %s on port %d", SERVICE_NAME, SERVICE_PORT)
    try:
        await redis_client.initialize()
    except Exception as exc:
        log.warning("Redis not available: %s", exc)
    try:
        await db_pool.initialize()
    except Exception as exc:
        log.warning("PostgreSQL not available: %s", exc)
    try:
        await config_provider.initialize()
    except Exception as exc:
        log.warning("ConfigProvider unavailable: %s", exc)

    # 2026-08-05 (D4): 启动 K线采集循环。
    # 生产环境唯一真实价源是 mt5_bridge 写入 Redis 的 market:latest:{symbol}；
    # collector 复用该价源自启聚合循环。PG 写入默认关闭（collector.kline_write_enabled
    # 或环境变量 COLLECTOR_KLINE_WRITE_ENABLED），避免与桥双写污染信号塔依赖的 klines。
    # 一旦有独立行情网关（hcm-gateway）或确认桥停止写 klines，可开启写入。
    global kline_collector
    try:
        symbols = ["XAUUSD"]
        timeframes = ["M1", "M5", "M15", "H1", "H4"]
        write_enabled = os.getenv(
            "COLLECTOR_KLINE_WRITE_ENABLED", "false"
        ).lower() in ("1", "true", "yes")
        # 尽力从配置中心读取覆盖值；读取失败则沿用默认/环境变量。
        try:
            if config_provider is not None:
                _sym = await config_provider.get_json("collector.symbols", symbols)
                _tf = await config_provider.get_json("collector.timeframes", timeframes)
                symbols = _sym if isinstance(_sym, list) and _sym else symbols
                timeframes = _tf if isinstance(_tf, list) and _tf else timeframes
                write_enabled = await config_provider.get_bool(
                    "collector.kline_write_enabled", write_enabled
                )
        except Exception as ce:
            log.warning("collector config read failed, using defaults: %s", ce)
        price_source = RedisPriceSource(redis_client)
        kline_collector = KlineCollector(
            db_pool=db_pool,
            redis_client=redis_client,
            config_provider=None,  # 配置已在 startup 读取，避免 start() 内部重复读导致启动失败
            price_source=price_source,
            pg_write_enabled=bool(write_enabled),
        )
        await kline_collector.start(symbols=symbols, timeframes=timeframes)
        log.info(
            "KlineCollector started (pg_write_enabled=%s, symbols=%s, timeframes=%s)",
            write_enabled, symbols, timeframes,
        )
    except Exception as exc:
        log.warning("KlineCollector start failed (non-fatal): %s", exc)


@app.on_event("shutdown")
async def shutdown():
    log.info("Shutting down %s", SERVICE_NAME)
    global kline_collector
    if kline_collector is not None:
        try:
            await kline_collector.stop()
        except Exception as exc:
            log.warning("KlineCollector stop failed: %s", exc)
        kline_collector = None
    await redis_client.shutdown()
    await db_pool.shutdown()


@app.get("/")
async def root():
    return {"service": SERVICE_NAME, "version": "2.0.0", "status": "running"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT, log_level="info")
