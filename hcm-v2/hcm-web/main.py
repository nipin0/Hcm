"""hcm-web — Web 管理后台 + 配置中心 + 数据看板

FastAPI + React SPA. 提供:
- RESTful 配置管理 API (14 个标签页)
- JWT 认证 + RBAC
- WebSocket 实时推送
- 数据看板聚合查询
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from shared.health import HealthChecker, create_health_router
from shared.logging_setup import setup_logging
from shared.redis_client import RedisClient
from shared.db import DatabasePool
from shared.config_provider import ConfigProviderV3

from web.api.auth import AuthHandler, create_auth_router
from web.api.config import create_config_router
from web.api.symbols import create_symbols_router
from web.api.signals import create_signals_router
from web.api.positions import create_positions_router
from web.api.dashboard import create_dashboard_router
from web.api.health_api import create_health_api_router
from web.ws.signal_push import create_signal_push_router

# ── Gap Implementation — New Route Imports ─────
# Modules created in T02-T05; imports are pre-declared here,
# include_router calls below are commented out and will be
# activated one-by-one as each module is implemented.
from web.api.copy import create_copy_router             # T02
from web.api.risk import create_risk_router             # T03
from web.api.signal_tower import create_signal_tower_router  # T03
from web.api.dispatch import create_dispatch_router     # T04
from web.api.close import create_close_router           # T04
from web.api.datasource import create_datasource_router # T04
from web.api.engine import create_engine_router         # T04
from web.api.system import create_system_router         # T05
from web.api.cosource import create_cosource_router    # P1a/P1b co-source config
from web.api.hexp import create_hexp_router            # 和乘幂(hexp) 独立信号源 config + live signal
from web.api.ai_config import create_ai_config_router  # AI 信号质量模块 ai.* 配置
from web.api.ai_report import create_ai_report_router  # AI 报表聚合（只读）

# ── Configuration ──────────────────────────────

SERVICE_NAME = "hcm-web"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8000"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
DB_URL = os.getenv("DB_URL", "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2")
JWT_SECRET = os.getenv("JWT_SECRET", "hcm-v2-dev-secret-change-in-production")
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend")

log = setup_logging(service_name=SERVICE_NAME, level="INFO")

# ── Dependencies ───────────────────────────────

health = HealthChecker(service_name=SERVICE_NAME, version="2.0.0")
redis_client = RedisClient(url=REDIS_URL)
db_pool = DatabasePool(dsn=DB_URL)
config_provider: ConfigProviderV3 = None
auth_handler: AuthHandler = None


async def check_redis():
    return await redis_client.health_check()


async def check_pg():
    return await db_pool.health_check()


health.add_check("redis", check_redis)
health.add_check("postgresql", check_pg)

# ── FastAPI Application ────────────────────────

app = FastAPI(
    title="HCM Web",
    description="HCM v2 Dashboard + Config Center + Data Analytics",
    version="2.0.0",
    # Disable built-in /openapi.json /docs /redoc so we can serve SPA at root
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static assets FIRST (before include_router + catch-all)
FRONTEND_DIST = os.path.join(FRONTEND_DIR, "dist") if os.path.isdir(FRONTEND_DIR) else None
if FRONTEND_DIST and os.path.isdir(os.path.join(FRONTEND_DIST, "assets")):
    from fastapi.staticfiles import StaticFiles as _SM
    app.mount("/assets", _SM(directory=os.path.join(FRONTEND_DIST, "assets")), name="assets")

# Health & Metrics
app.include_router(create_health_router(health))


@app.on_event("startup")
async def startup():
    global config_provider, auth_handler
    log.info("Starting %s on port %d", SERVICE_NAME, SERVICE_PORT)

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

    # Initialize auth handler
    auth_handler = AuthHandler(
        db_pool=db_pool,
        config_provider=config_provider,
        jwt_secret=JWT_SECRET,
    )
    await auth_handler.load_config()
    log.info("AuthHandler initialized")

    # Register API routes
    app.include_router(create_auth_router(auth_handler))
    app.include_router(create_config_router(db_pool, config_provider, auth_handler, redis_client))
    app.include_router(create_symbols_router(db_pool, config_provider, auth_handler))
    app.include_router(create_signals_router(db_pool, redis_client, auth_handler))
    app.include_router(create_positions_router(db_pool, redis_client, auth_handler))
    dash_router, dash_legacy_router = create_dashboard_router(db_pool, redis_client, auth_handler)
    app.include_router(dash_router)
    app.include_router(dash_legacy_router)
    app.include_router(create_health_api_router(db_pool, redis_client, health, auth_handler))
    app.include_router(create_signal_push_router(redis_client, auth_handler))
    log.info("All API routes registered")

    # ── Gap Implementation — New Route Registrations ─────
    # T02: Copy trading — relationships + symbol mappings + trade logs
    copy_router, copy_legacy_router = create_copy_router(db_pool, config_provider, auth_handler)
    app.include_router(copy_router)
    app.include_router(copy_legacy_router)
    # T03: Risk control — config + symbol limits + intercept logs
    app.include_router(create_risk_router(db_pool, config_provider, auth_handler, redis_client))
    # T03: Signal tower — cooldown config + dual-mode + symbol tower config
    app.include_router(create_signal_tower_router(db_pool, config_provider, auth_handler))
    # T04: Dispatch config — order dispatch configuration
    app.include_router(create_dispatch_router(db_pool, config_provider, auth_handler))
    # T04: Close config — position close configuration
    app.include_router(create_close_router(db_pool, config_provider, auth_handler))
    # T04: Datasource config — data source configuration
    app.include_router(create_datasource_router(db_pool, config_provider, auth_handler))
    # T04: Engine rules — inference engine rules CRUD
    app.include_router(create_engine_router(db_pool, config_provider, auth_handler))
    # T05: System management — users CRUD + MT5/DeepSeek/Network/Notifications/Cache (+ redis for cache)
    app.include_router(create_system_router(db_pool, config_provider, auth_handler, redis_client))
    # P1a/P1b: Co-source signal enhancement — calibration + filters + adaptive gates config CRUD
    app.include_router(create_cosource_router(db_pool, config_provider, auth_handler))
    # 和乘幂(hexp) 独立信号源 — config CRUD + 实时信号快照(需 redis 读 hcm:live:hexp:*)
    app.include_router(create_hexp_router(db_pool, config_provider, auth_handler, redis_client))
    # AI 信号质量模块 — ai.* 配置 CRUD（LightGBM/DeepSeek/耦合/持仓调仓）
    app.include_router(create_ai_config_router(db_pool, config_provider, auth_handler))
    # AI 报表聚合 — 系统健康/信号分层/绩效对比/快照明细（只读）
    app.include_router(create_ai_report_router(db_pool, auth_handler))

    # Mount frontend static files at root, BUT keep /docs /openapi.json for backend
    if FRONTEND_DIST and os.path.isdir(FRONTEND_DIST):
        from starlette.responses import FileResponse as _FR
        from starlette.exceptions import HTTPException as _HE

        @app.get("/", include_in_schema=False)
        async def serve_root():
            return _FR(os.path.join(FRONTEND_DIST, "index.html"))

        @app.get("/vite.svg", include_in_schema=False)
        async def serve_vite():
            p = os.path.join(FRONTEND_DIST, "vite.svg")
            if os.path.isfile(p): return _FR(p)
            raise _HE(404)

        @app.get("/favicon.ico", include_in_schema=False)
        async def serve_favicon():
            p = os.path.join(FRONTEND_DIST, "favicon.ico")
            if os.path.isfile(p): return _FR(p)
            raise _HE(404)

        # Catch-all for SPA client-side routes
        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa_catch_all(full_path: str):
            if full_path.startswith(("api", "docs", "openapi.json", "redoc", "ws")):
                raise _HE(404, "Not found")
            return _FR(os.path.join(FRONTEND_DIST, "index.html"))
        log.info("Frontend static files served from %s", FRONTEND_DIST)

    log.info("%s started successfully", SERVICE_NAME)


@app.on_event("shutdown")
async def shutdown():
    log.info("Shutting down %s", SERVICE_NAME)
    if config_provider:
        await config_provider.shutdown()
    await redis_client.shutdown()
    await db_pool.shutdown()


# ── API Routes ─────────────────────────────────
# Note: Root endpoint removed; SPA frontend handles "/" in startup()
# API overview available at /api/status


# ── API Info ───────────────────────────────────

@app.get("/api/v1/version")
async def api_version():
    """API version info."""
    return {
        "code": 0,
        "data": {
            "api_version": "v1",
            "service_version": "2.0.0",
            "service_name": SERVICE_NAME,
        },
        "message": "ok",
    }


# ── Main ───────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    # Read bind host from config_provider if available, else env, else 0.0.0.0
    host = os.getenv("SERVICE_HOST", "0.0.0.0")
    try:
        # Late import: avoid loading at module-import time (may not be ready)
        import redis as _redis_mod
        _r = _redis_mod.Redis(host="localhost", port=6379, decode_responses=True)
        cfg_host = _r.hget("hcm:config:v2", "service_host")
        if cfg_host:
            host = cfg_host
        cfg_port = _r.hget("hcm:config:v2", "service_port")
        if cfg_port:
            SERVICE_PORT = int(cfg_port)
    except Exception:
        pass
    log.info("Starting %s on %s:%d", SERVICE_NAME, host, SERVICE_PORT)
    uvicorn.run(app, host=host, port=SERVICE_PORT, log_level="info")
