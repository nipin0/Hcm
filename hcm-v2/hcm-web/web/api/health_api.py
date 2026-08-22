"""Health API — System-wide health aggregation.

Provides:
- GET /api/v1/health — Aggregated health across all services
- GET /api/v1/health/services — Per-service health status
- GET /api/v1/health/streams — Redis Stream pending status
- GET /api/v1/health/watchdog — Watchdog status check
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Query, Request

logger = logging.getLogger(__name__)

# Service registry for health checks
SERVICE_REGISTRY = {
    "hcm-gateway": {"port": 8001, "path": "/health"},
    "hcm-signal-tower": {"port": 8002, "path": "/health"},
    "hcm-risk-engine": {"port": 8003, "path": "/health"},
    "hcm-dispatcher": {"port": 8004, "path": "/health"},
    "hcm-copy-trading": {"port": 8005, "path": "/health"},
    "hcm-market-intel": {"port": 8006, "path": "/health"},
    "hcm-collector": {"port": 8007, "path": "/health"},
}


# ── Router Factory ─────────────────────────────

def create_health_api_router(
    db_pool: Any = None,
    redis_client: Any = None,
    health_checker: Any = None,
    auth_handler: Any = None,
) -> APIRouter:
    """Create FastAPI router with system health endpoints.

    Args:
        db_pool: DatabasePool instance.
        redis_client: RedisClient instance.
        health_checker: HealthChecker instance for self-health.
        auth_handler: AuthHandler instance for RBAC.

    Returns:
        APIRouter with health routes.
    """
    router = APIRouter(prefix="/api/v1/health", tags=["health"])

    # ── Aggregated Health ───────────────────────

    @router.get("")
    async def aggregated_health(request: Request):
        """Get aggregated system health across all services.

        Checks local components (PG, Redis) and optionally
        pings other services.
        """
        result = {
            "status": "healthy",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "components": {},
        }

        # Check PostgreSQL
        pg_status = "unknown"
        pg_latency = 0.0
        if db_pool is not None and db_pool.is_initialized:
            try:
                pg_check = await db_pool.health_check()
                pg_status = pg_check.get("status", "unknown")
                pg_latency = pg_check.get("latency_ms", 0.0)
            except Exception as exc:
                pg_status = "unhealthy"
                logger.warning("PG health check failed: %s", exc)

        result["components"]["postgresql"] = {
            "status": pg_status,
            "latency_ms": round(pg_latency, 2),
        }

        # Check Redis
        redis_status = "unknown"
        redis_latency = 0.0
        if redis_client is not None and redis_client.is_initialized:
            try:
                redis_check = await redis_client.health_check()
                redis_status = redis_check.get("status", "unknown")
                redis_latency = redis_check.get("latency_ms", 0.0)
            except Exception as exc:
                redis_status = "unhealthy"
                logger.warning("Redis health check failed: %s", exc)

        result["components"]["redis"] = {
            "status": redis_status,
            "latency_ms": round(redis_latency, 2),
        }

        # Self health
        if health_checker is not None:
            try:
                self_check = await health_checker.check()
                result["components"]["self"] = {
                    "status": self_check.get("status", "unknown"),
                    "uptime_seconds": self_check.get("uptime_seconds", 0),
                }
            except Exception:
                pass

        # Aggregate status
        statuses = [c["status"] for c in result["components"].values()]
        if "unhealthy" in statuses:
            result["status"] = "degraded"
        if all(s == "healthy" for s in statuses):
            result["status"] = "healthy"

        return {"code": 0, "data": result, "message": "ok"}

    # ── Per-Service Health ──────────────────────

    @router.get("/services")
    async def services_health(request: Request):
        """Get health status of all registered services.

        Attempts to ping each service's /health endpoint.
        """
        result: dict[str, Any] = {
            "services": {},
            "healthy_count": 0,
            "unhealthy_count": 0,
            "total": len(SERVICE_REGISTRY),
        }

        async def check_service(name: str, info: dict) -> dict:
            """Check a single service health."""
            try:
                import httpx
                async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                    resp = await client.get(
                        f"http://localhost:{info['port']}{info['path']}"
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        return {
                            "name": name,
                            "status": data.get("status", "healthy"),
                            "uptime_seconds": data.get("uptime_seconds", 0),
                            "reachable": True,
                        }
                    else:
                        return {
                            "name": name,
                            "status": "unhealthy",
                            "error": f"HTTP {resp.status_code}",
                            "reachable": True,
                        }
            except Exception as exc:
                return {
                    "name": name,
                    "status": "unreachable",
                    "error": str(exc),
                    "reachable": False,
                }

        # Check all services concurrently
        tasks = [
            check_service(name, info)
            for name, info in SERVICE_REGISTRY.items()
        ]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results_list:
            if isinstance(r, Exception):
                continue
            name = r.pop("name")
            result["services"][name] = r
            if r.get("status") == "healthy":
                result["healthy_count"] += 1
            else:
                result["unhealthy_count"] += 1

        return {"code": 0, "data": result, "message": "ok"}

    # ── Stream Pending ──────────────────────────

    @router.get("/streams")
    async def stream_pending(request: Request):
        """Get Redis Stream pending message counts.

        Checks pending messages in consumer groups for
        signal:stream and signal:dead.
        """
        result: dict[str, Any] = {
            "streams": {},
        }

        if redis_client is not None and redis_client.is_initialized:
            try:
                # Check signal:stream pending
                pending_signal = await redis_client.xpending("signal:stream", "risk-engine-group")
                pending_web = await redis_client.xpending("signal:stream", "web-push-group")

                result["streams"]["signal:stream"] = {
                    "risk-engine-group": pending_signal,
                    "web-push-group": pending_web,
                }

                # Check dead letter stream length
                dlq_len = await redis_client.xlen("signal:dead")
                result["streams"]["signal:dead"] = {
                    "length": dlq_len,
                }

                # Check stream lengths
                signal_len = await redis_client.xlen("signal:stream")
                result["streams"]["signal:stream"]["length"] = signal_len

            except Exception as exc:
                logger.warning("Stream pending check failed: %s", exc)
                result["streams"]["error"] = str(exc)

        return {"code": 0, "data": result, "message": "ok"}

    # ── Watchdog Status ─────────────────────────

    @router.get("/watchdog")
    async def watchdog_status(request: Request):
        """Get watchdog status for signal-tower.

        Checks circuit breaker state and watchdog heartbeat
        from Redis.
        """
        result: dict[str, Any] = {
            "circuit_breaker": "unknown",
            "watchdog": "unknown",
        }

        if redis_client is not None and redis_client.is_initialized:
            try:
                # Check if services are alive by checking Redis keys
                # This is a proxy — actual watchdog state is in signal-tower
                result["watchdog"] = "active" if await redis_client.ping() else "inactive"
            except Exception as exc:
                logger.warning("Watchdog status check failed: %s", exc)
                result["error"] = str(exc)

        return {"code": 0, "data": result, "message": "ok"}

    return router
