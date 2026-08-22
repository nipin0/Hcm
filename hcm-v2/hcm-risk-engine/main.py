"""hcm-risk-engine — 风控引擎

Redis Stream 消费者: signal:stream → 规则链校验 → 决策(通过/拦截/降级)
风控通过后 XADD signal:risk_passed，拦截后记录 block_reason。

事件驱动消费链路:
  1. XGROUP CREATE risk-engine-group on signal:stream (idempotent)
  2. XREADGROUP 消费信号，先恢复 pending 消息
  3. RuleChain 九道规则链串行校验 (从 ConfigProviderV3 读取阈值)
  4. DecisionEngine PASS/REJECT/DEGRADE 决策
  5. PASS → XADD signal:risk_passed → ACK
  6. REJECT → 记录原因 → ACK
  7. 重试 3 次失败 → signal:dead 死信队列
"""

import asyncio
import os
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import FastAPI

from shared.health import HealthChecker, create_health_router
from shared.logging_setup import setup_logging
from shared.redis_client import RedisClient
from shared.db import DatabasePool
from shared.config_provider import ConfigProviderV3

from risk_engine.stream_consumer import RiskStreamConsumer, RiskConsumerConfig
from risk_engine.rule_chain import RuleChain
from risk_engine.decision import DecisionEngine

# ── Configuration ──────────────────────────────

SERVICE_NAME = "hcm-risk-engine"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8007"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DB_URL = os.getenv("DB_URL", "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2")
CONSUMER_NAME = os.getenv("RISK_CONSUMER_NAME", "risk-consumer-1")

# ── Logging ────────────────────────────────────

log = setup_logging(service_name=SERVICE_NAME, level="INFO")

# ── Dependencies ───────────────────────────────

health = HealthChecker(service_name=SERVICE_NAME, version="2.0.0")
redis_client = RedisClient(url=REDIS_URL)
db_pool = DatabasePool(dsn=DB_URL)
config_provider: ConfigProviderV3 = None  # initialized in startup

# Risk engine components
rule_chain: RuleChain = None
decision_engine: DecisionEngine = None
stream_consumer: RiskStreamConsumer = None


# ── Health Checks ──────────────────────────────

async def check_redis():
    return await redis_client.health_check()

async def check_pg():
    return await db_pool.health_check()

async def check_risk_engine():
    if stream_consumer:
        return await stream_consumer.health_check()
    return {"status": "not_started"}

health.add_check("redis", check_redis)
health.add_check("postgresql", check_pg)
health.add_check("risk_engine", check_risk_engine)


# ── Background: hot-reload risk config (mirrors scheduler._refresh_deepseek_config) ──
_rule_config_refresh_task: Optional[asyncio.Task] = None


async def _rule_config_refresh_loop() -> None:
    """Periodically reload risk thresholds from ConfigProviderV3.

    load_config() is resilient (never raises), so a transient config outage
    only keeps last-good thresholds. This makes backend config changes take
    effect without restarting the risk-engine container.
    """
    while True:
        await asyncio.sleep(60)
        try:
            if rule_chain is not None:
                await rule_chain.load_config()
        except Exception as exc:  # noqa: BLE001
            log.warning("Rule config hot-reload error: %s", exc)


# ── FastAPI ────────────────────────────────────

app = FastAPI(
    title="HCM Risk Engine",
    description="Rule chain validator for trading signals — PASS/REJECT/DEGRADE",
    version="2.0.0",
)
app.include_router(create_health_router(health))


@app.on_event("startup")
async def startup():
    global config_provider, rule_chain, decision_engine, stream_consumer, _rule_config_refresh_task

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

    # Initialize rule chain
    rule_chain = RuleChain(
        config_provider=config_provider,
        db_pool=db_pool,
        redis_client=redis_client,
    )
    await rule_chain.load_config()

    # Initialize decision engine
    decision_engine = DecisionEngine(config_provider=config_provider)
    await decision_engine.load_config()

    # Initialize stream consumer
    consumer_config = RiskConsumerConfig(consumer_name=CONSUMER_NAME)
    stream_consumer = RiskStreamConsumer(
        redis_client=redis_client,
        rule_chain=rule_chain,
        decision_engine=decision_engine,
        config=consumer_config,
        db_pool=db_pool,
    )
    await stream_consumer.start()

    # Hot-reload risk config every 60s (no restart needed when PG config changes)
    global _rule_config_refresh_task
    _rule_config_refresh_task = asyncio.create_task(_rule_config_refresh_loop())
    log.info("Risk config hot-reload loop started (60s)")

    log.info("%s fully initialized", SERVICE_NAME)


@app.on_event("shutdown")
async def shutdown():
    log.info("Shutting down %s", SERVICE_NAME)

    if stream_consumer:
        await stream_consumer.stop()
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
        "endpoints": {
            "health": "/health",
            "metrics": "/metrics",
            "ready": "/ready",
            "live": "/live",
        },
    }


@app.get("/stats")
async def stats():
    """Get risk engine statistics."""
    if stream_consumer:
        return {
            "service": SERVICE_NAME,
            "risk_consumer": stream_consumer.get_stats(),
        }
    return {"service": SERVICE_NAME, "status": "not_started"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT, log_level="info")
