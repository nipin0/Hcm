"""hcm-market-intel — 市场情报服务

四合一采集（宏观/情绪/事件/流动性）+ AI 赋分。
按品种类别区分因子采集逻辑和赋分模板。
采集时一次性调用 DeepSeek 赋分 → Redis 缓存 + PG 快照。
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import FastAPI

from shared.health import HealthChecker, create_health_router
from shared.logging_setup import setup_logging
from shared.redis_client import RedisClient
from shared.db import DatabasePool
from shared.config_provider import ConfigProviderV3

from market_intel.ai_scorer import AiScorer
from market_intel.macro_collector import MacroCollector
from market_intel.sentiment_collector import SentimentCollector
from market_intel.event_calendar import EventCalendar
from market_intel.liquidity_analyzer import LiquidityAnalyzer

# ── Configuration ──────────────────────────────

SERVICE_NAME = "hcm-market-intel"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8006"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DB_URL = os.getenv("DB_URL", "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
STUB_MODE = os.getenv("MARKET_INTEL_STUB_MODE", "true").lower() in ("true", "1", "yes")

# ── Logging ────────────────────────────────────

log = setup_logging(service_name=SERVICE_NAME, level="INFO")

# ── Dependencies ───────────────────────────────

health = HealthChecker(service_name=SERVICE_NAME, version="2.0.0")
redis_client = RedisClient(url=REDIS_URL)
db_pool = DatabasePool(dsn=DB_URL)
config_provider: ConfigProviderV3 = None

# Market intel modules
ai_scorer: AiScorer = None
macro_collector: MacroCollector = None
sentiment_collector: SentimentCollector = None
event_calendar: EventCalendar = None
liquidity_analyzer: LiquidityAnalyzer = None

# Background tasks
_collection_task: asyncio.Task = None
_running: bool = False


# ── Health Checks ──────────────────────────────

async def check_redis():
    return await redis_client.health_check()


async def check_pg():
    return await db_pool.health_check()


health.add_check("redis", check_redis)
health.add_check("postgresql", check_pg)

# ── FastAPI ────────────────────────────────────

app = FastAPI(
    title="HCM Market Intel",
    description="Macro/Sentiment/Event/Liquidity collector + AI scoring",
    version="2.0.0",
)
app.include_router(create_health_router(health))


@app.on_event("startup")
async def startup():
    global config_provider, ai_scorer, macro_collector
    global sentiment_collector, event_calendar, liquidity_analyzer, _running, _collection_task

    log.info("Starting %s on port %d (stub_mode=%s)", SERVICE_NAME, SERVICE_PORT, STUB_MODE)

    try:
        await redis_client.initialize()
    except Exception as exc:
        log.warning("Redis not available: %s", exc)

    try:
        await db_pool.initialize()
    except Exception as exc:
        log.warning("PostgreSQL not available: %s", exc)

    config_provider = ConfigProviderV3(
        pg_pool=db_pool,
        redis_client=redis_client.raw if redis_client.is_initialized else None,
    )
    await config_provider.initialize()
    log.info("ConfigProviderV3 initialized")

    # Resolve the effective DeepSeek key: prefer the env var, but fall back to
    # the central config (PG/Redis deepseek.api_key) so the AI scorer uses the
    # same real key the rest of the system already has — even when the env file
    # leaves DEEPSEEK_API_KEY blank.
    effective_ds_key = DEEPSEEK_API_KEY
    if not effective_ds_key and config_provider is not None:
        try:
            cfg_val = await config_provider.get("deepseek.api_key")
            if cfg_val:
                effective_ds_key = str(cfg_val)
                log.info("DeepSeek API key loaded from config provider (deepseek.api_key)")
        except Exception as exc:
            log.warning("Failed to read deepseek.api_key from config provider: %s", exc)
    effective_ds_base = DEEPSEEK_BASE_URL
    if not effective_ds_base and config_provider is not None:
        try:
            cfg_base = await config_provider.get("deepseek.api_base")
            if cfg_base:
                effective_ds_base = str(cfg_base)
        except Exception:
            pass

    # Initialize AI Scorer
    ai_scorer = AiScorer(
        api_key=effective_ds_key,
        base_url=effective_ds_base,
        stub_mode=STUB_MODE or not effective_ds_key,
    )
    await ai_scorer.initialize()
    log.info("AiScorer initialized (stub=%s)", ai_scorer.is_stub_mode)

    # Initialize collectors
    macro_collector = MacroCollector(
        db_pool=db_pool,
        redis_client=redis_client,
        ai_scorer=ai_scorer,
        config_provider=config_provider,
        stub_mode=STUB_MODE,
    )
    sentiment_collector = SentimentCollector(
        db_pool=db_pool,
        redis_client=redis_client,
        ai_scorer=ai_scorer,
        config_provider=config_provider,
        stub_mode=STUB_MODE,
    )
    event_calendar = EventCalendar(
        db_pool=db_pool,
        redis_client=redis_client,
        ai_scorer=ai_scorer,
        config_provider=config_provider,
    )
    liquidity_analyzer = LiquidityAnalyzer(
        redis_client=redis_client,
        config_provider=config_provider,
    )

    # Load configs
    await macro_collector.load_config()
    await sentiment_collector.load_config()
    await event_calendar.load_config()
    await liquidity_analyzer.load_config()

    # Load events
    await event_calendar.load_events()

    # Start four independent collection loops with per-collector intervals
    _running = True
    _collection_task = asyncio.create_task(_macro_loop())
    asyncio.create_task(_sentiment_loop())
    asyncio.create_task(_event_loop())
    asyncio.create_task(_liquidity_loop())
    # Composite scorer: lightweight aggregator (no AI cost)
    asyncio.create_task(_composite_score_loop())

    # Persist status flags so the web panel can distinguish a genuinely
    # active collector from placeholder (stub) data.
    #
    # hcm:market:stub_mode  -> true ONLY when MARKET_INTEL_STUB_MODE env is on
    #   (all dimensions return placeholder/zero). When the env is false the
    #   collectors run real logic (e.g. metals read genuine PG K-lines), so the
    #   service is considered active even if the AI final-scoring falls back to
    #   heuristics (no API key).
    # hcm:market:ai_offline -> true when no DEEPSEEK_API_KEY is configured, so
    #   the panel can note "AI scoring uses heuristics" without marking the
    #   whole service as stub.
    try:
        if redis_client and redis_client.is_initialized:
            effective_stub = bool(STUB_MODE)
            ai_offline = not effective_ds_key
            await redis_client.set("hcm:market:stub_mode", str(effective_stub).lower())
            await redis_client.set("hcm:market:ai_offline", str(ai_offline).lower())
            await redis_client.set("hcm:market:last_collection", datetime.now(timezone.utc).isoformat())
            log.info("Market-intel status flags written (stub=%s, ai_offline=%s)",
                      effective_stub, ai_offline)
    except Exception as exc:
        log.warning("Failed to write market-intel status flags: %s", exc)

    log.info("%s started successfully", SERVICE_NAME)


@app.on_event("shutdown")
async def shutdown():
    global _running
    log.info("Shutting down %s", SERVICE_NAME)

    _running = False
    if _collection_task:
        _collection_task.cancel()
    # Cancel all running coroutine tasks gracefully
    for t in asyncio.all_tasks():
        if t is not asyncio.current_task():
            t.cancel()

    if ai_scorer:
        await ai_scorer.shutdown()
    if config_provider:
        await config_provider.shutdown()
    await redis_client.shutdown()
    await db_pool.shutdown()


# ── Multi-frequency Collection Tasks ────────────

async def _get_interval_seconds(config_key: str, default_seconds: int) -> int:
    """Read collector interval from Redis config, fallback to default."""
    try:
        if config_provider:
            val = await config_provider.get_int(config_key, default_seconds)
            return max(val, 10)  # minimum 10s safety floor
    except Exception:
        pass
    return default_seconds


async def _write_score_to_redis(name: str, score: float) -> None:
    """Write collector score to Redis for pipeline display.

    Also refreshes hcm:market:last_collection heartbeat so the web panel
    can detect a stalled collector.
    """
    try:
        if redis_client and redis_client.is_initialized:
            await redis_client.set(f"hcm:market:{name}:score", str(round(score, 3)))
            await redis_client.set("hcm:market:last_collection", datetime.now(timezone.utc).isoformat())
    except Exception:
        pass


async def _macro_loop() -> None:
    """Macro collector: daily interval."""
    log.info("Macro loop started (default 86400s)")
    while _running:
        try:
            # collect_all() returns {category: snapshot_id}; the real score lives
            # in the persisted snapshot fetched via get_latest_all().
            await macro_collector.collect_all()
            latest = await macro_collector.get_latest_all()
            raw = [float(s["score"]) for s in latest.values()
                   if s and isinstance(s.get("score"), (int, float))]
            if raw:
                raw_avg = sum(raw) / len(raw)
                # collectors emit a 0-30 scale; the composite expects 0-1
                score = raw_avg / 30.0
            else:
                score = 0.5
            await _write_score_to_redis("macro", score)
            log.info("Macro collected: normalized=%.2f (raw_avg=%.1f/30)", score,
                     raw_avg if raw else 0.0)
        except Exception as exc:
            log.warning("Macro collection failed: %s", exc)
        interval = await _get_interval_seconds("market_macro_interval_s", 86400)
        await asyncio.sleep(interval)


async def _sentiment_loop() -> None:
    """Sentiment collector: 6h interval, checks for updates."""
    log.info("Sentiment loop started (default 21600s)")
    while _running:
        try:
            await sentiment_collector.collect_all()
            latest = await sentiment_collector.get_latest_all()
            raw = [float(s["score"]) for s in latest.values()
                   if s and isinstance(s.get("score"), (int, float))]
            if raw:
                raw_avg = sum(raw) / len(raw)
                # collectors emit a 0-20 scale; the composite expects 0-1
                score = raw_avg / 20.0
            else:
                score = 0.5
            await _write_score_to_redis("sentiment", score)
            log.info("Sentiment collected: normalized=%.2f (raw_avg=%.1f/20)", score,
                     raw_avg if raw else 0.0)
        except Exception as exc:
            log.warning("Sentiment collection failed: %s", exc)
        interval = await _get_interval_seconds("market_sentiment_interval_s", 21600)
        await asyncio.sleep(interval)


async def _event_loop() -> None:
    """Event calendar: 12h interval + active warning for upcoming events."""
    log.info("Event loop started (default 43200s)")
    while _running:
        try:
            results = await event_calendar.check_all()
            # count HIGH/CRITICAL events currently upcoming or active
            urgent = 0
            for states in results.values():
                for st in states:
                    if st.event.importance >= 2 and st.status in ("upcoming", "active"):
                        urgent += 1
            score = min(1.0, 0.3 + urgent * 0.2) if urgent else 0.2
            await _write_score_to_redis("event", score)
            log.info("Event check: urgent=%d score=%.2f", urgent, score)
        except Exception as exc:
            log.warning("Event check failed: %s", exc)
        interval = await _get_interval_seconds("market_event_interval_s", 43200)
        await asyncio.sleep(interval)


async def _liquidity_loop() -> None:
    """Liquidity analyzer: real-time from bridge Redis, 60s refresh."""
    log.info("Liquidity loop started (default 60s)")
    while _running:
        try:
            # Read bid/ask from bridge's Redis key
            score = 0.5
            if redis_client and redis_client.is_initialized:
                raw = await redis_client.get("hcm:market:liquidity:score")
                if raw is None:
                    # Compute from bid/ask spread if available
                    price_raw = await redis_client.hget("hcm:config:v2", "market:latest:XAUUSD")
                    if price_raw:
                        import json
                        data = json.loads(price_raw)
                        bid = float(data.get("bid", 0))
                        ask = float(data.get("ask", 0))
                        if bid > 0 and ask > bid:
                            spread_pct = (ask - bid) / bid * 100
                            if spread_pct < 0.02:
                                score = 0.1  # very tight
                            elif spread_pct < 0.05:
                                score = 0.3
                            elif spread_pct < 0.10:
                                score = 0.5
                            else:
                                score = 0.8  # wide spread = low liquidity
                else:
                    score = float(raw)
            await _write_score_to_redis("liquidity", score)
        except Exception as exc:
            log.warning("Liquidity analysis failed: %s", exc)
        interval = await _get_interval_seconds("market_liquidity_interval_s", 60)
        await asyncio.sleep(interval)


async def _composite_score_loop() -> None:
    """Lightweight composite scorer — aggregates 4 dimension scores
    into a single 0-1 value, written to Redis every 30s.
    No AI calls — just weighted average from Redis cache."""
    log.info("Composite score loop started")
    while _running:
        try:
            composite = 0.5  # neutral fallback
            if redis_client and redis_client.is_initialized:
                scores: dict[str, float] = {}
                for dim in ("macro", "sentiment", "event", "liquidity"):
                    raw = await redis_client.get(f"hcm:market:{dim}:score")
                    if raw:
                        scores[dim] = float(raw)
                if scores:
                    # Weighted: macro=0.35 sentiment=0.20 event=0.25 liquidity=0.20
                    weights = {"macro": 0.35, "sentiment": 0.20, "event": 0.25, "liquidity": 0.20}
                    composite = sum(scores.get(k, 0.5) * weights.get(k, 0.25) for k in scores)
                    composite = round(composite, 3)
            await _write_score_to_redis("composite", composite)
        except Exception:
            pass
        await asyncio.sleep(30)


# ── API Routes ─────────────────────────────────

@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "service": SERVICE_NAME,
        "version": "2.0.0",
        "status": "running",
        "stub_mode": STUB_MODE,
        "ai_scorer_stub": ai_scorer.is_stub_mode if ai_scorer else True,
    }


@app.get("/api/v1/market-intel/macro/{category}")
async def get_macro(category: str):
    """Get latest macro snapshot for a category."""
    if macro_collector is None:
        return {"code": "SERVICE_NOT_READY", "data": None, "message": "Service not ready"}
    try:
        result = await macro_collector.get_latest(category)
        return {"code": 0, "data": result, "message": "ok"}
    except Exception as exc:
        return {"code": "MI_MACRO_001", "data": None, "message": str(exc)}


@app.get("/api/v1/market-intel/macro")
async def get_all_macro():
    """Get latest macro snapshots for all categories."""
    if macro_collector is None:
        return {"code": "SERVICE_NOT_READY", "data": None, "message": "Service not ready"}
    try:
        result = await macro_collector.get_latest_all()
        return {"code": 0, "data": result, "message": "ok"}
    except Exception as exc:
        return {"code": "MI_MACRO_001", "data": None, "message": str(exc)}


@app.get("/api/v1/market-intel/sentiment/{category}")
async def get_sentiment(category: str):
    """Get latest sentiment snapshot for a category."""
    if sentiment_collector is None:
        return {"code": "SERVICE_NOT_READY", "data": None, "message": "Service not ready"}
    try:
        result = await sentiment_collector.get_latest(category)
        return {"code": 0, "data": result, "message": "ok"}
    except Exception as exc:
        return {"code": "MI_SENT_001", "data": None, "message": str(exc)}


@app.get("/api/v1/market-intel/sentiment")
async def get_all_sentiment():
    """Get latest sentiment snapshots for all categories."""
    if sentiment_collector is None:
        return {"code": "SERVICE_NOT_READY", "data": None, "message": "Service not ready"}
    try:
        result = await sentiment_collector.get_latest_all()
        return {"code": 0, "data": result, "message": "ok"}
    except Exception as exc:
        return {"code": "MI_SENT_001", "data": None, "message": str(exc)}


@app.get("/api/v1/market-intel/events")
async def get_upcoming_events(category: str = "all", hours: int = 24):
    """Get upcoming economic events."""
    if event_calendar is None:
        return {"code": "SERVICE_NOT_READY", "data": None, "message": "Service not ready"}
    try:
        events = await event_calendar.get_upcoming_events(category=category, hours_ahead=hours)
        restrictions = await event_calendar.get_event_restrictions(category=category)
        return {
            "code": 0,
            "data": {
                "events": events,
                "restrictions": restrictions,
            },
            "message": "ok",
        }
    except Exception as exc:
        return {"code": "MI_EVT_001", "data": None, "message": str(exc)}


@app.get("/api/v1/market-intel/liquidity")
async def get_liquidity(symbol: str = ""):
    """Get current liquidity data."""
    if liquidity_analyzer is None:
        return {"code": "SERVICE_NOT_READY", "data": None, "message": "Service not ready"}
    try:
        result = await liquidity_analyzer.get_current(symbol=symbol)
        return {"code": 0, "data": result, "message": "ok"}
    except Exception as exc:
        return {"code": "MI_LIQ_001", "data": None, "message": str(exc)}


@app.post("/api/v1/market-intel/collect")
async def trigger_collection():
    """Manually trigger a full collection cycle."""
    if macro_collector is None or sentiment_collector is None:
        return {"code": "SERVICE_NOT_READY", "data": None, "message": "Service not ready"}
    try:
        macro_results = await macro_collector.collect_all()
        sentiment_results = await sentiment_collector.collect_all()
        await event_calendar.check_all()
        return {
            "code": 0,
            "data": {
                "macro": macro_results,
                "sentiment": sentiment_results,
            },
            "message": "Collection triggered",
        }
    except Exception as exc:
        return {"code": "SYS_NET_001", "data": None, "message": str(exc)}


@app.get("/api/v1/market-intel/stats")
async def get_stats():
    """Get service statistics."""
    return {
        "code": 0,
        "data": {
            "ai_scorer": ai_scorer.stats if ai_scorer else {},
            "macro_collector": macro_collector.get_stats() if macro_collector else {},
            "sentiment_collector": sentiment_collector.get_stats() if sentiment_collector else {},
            "event_calendar": event_calendar.get_stats() if event_calendar else {},
            "liquidity_analyzer": liquidity_analyzer.get_stats() if liquidity_analyzer else {},
        },
        "message": "ok",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT, log_level="info")
