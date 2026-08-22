"""Standard /health endpoint middleware for all HCM v2 services.

Provides a FastAPI-compatible health check endpoint that reports:
- Service status (healthy / degraded / unhealthy)
- Uptime
- Component checks (PostgreSQL, Redis, etc.)

Usage (in service main.py):
    from shared.health import HealthChecker, create_health_router
    from fastapi import FastAPI

    health = HealthChecker(service_name="hcm-collector")
    app = FastAPI()
    app.include_router(create_health_router(health))
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

try:
    from fastapi import APIRouter, Request
    from fastapi.responses import JSONResponse
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


@dataclass
class ComponentCheck:
    """Result of a single component health check."""
    name: str
    status: str = "healthy"     # healthy / degraded / unhealthy
    latency_ms: float = 0.0
    error: str = ""


@dataclass
class HealthChecker:
    """Aggregate health checker for a service.

    Example:
        health = HealthChecker(service_name="hcm-collector")

        async def check_db():
            return await db.health_check()

        async def check_redis():
            return await redis_cli.health_check()

        health.add_check("postgresql", check_db)
        health.add_check("redis", check_redis)

        status = await health.check()
    """

    service_name: str
    version: str = "2.0.0"
    _start_time: float = field(default_factory=time.time)
    _checks: dict[str, Callable] = field(default_factory=dict)

    def add_check(self, name: str, check_fn: Callable) -> None:
        """Register a component health check function.

        Args:
            name: Component name (e.g., "postgresql").
            check_fn: Async callable returning a dict with status/latency_ms/error.
        """
        self._checks[name] = check_fn

    async def check(self) -> dict:
        """Run all health checks and return aggregated status.

        Returns:
            Dict with overall status, uptime, and component-level results.
        """
        components: dict[str, dict] = {}
        overall_healthy = True

        for name, check_fn in self._checks.items():
            try:
                result = await check_fn()
            except Exception as exc:
                result = {"status": "unhealthy", "error": str(exc), "latency_ms": 0}

            components[name] = result
            if result.get("status", "unhealthy") != "healthy":
                overall_healthy = False

        uptime = time.time() - self._start_time
        return {
            "status": "healthy" if overall_healthy else "degraded",
            "service": self.service_name,
            "version": self.version,
            "uptime_seconds": round(uptime, 1),
            "checks": components,
        }

    def get_uptime(self) -> float:
        """Get service uptime in seconds."""
        return time.time() - self._start_time


def create_health_router(health: HealthChecker) -> Any:
    """Create a FastAPI router with /health and /metrics endpoints.

    Args:
        health: HealthChecker instance.

    Returns:
        FastAPI APIRouter with health endpoints.
    """
    if not HAS_FASTAPI:
        raise ImportError("FastAPI is required for create_health_router")

    router = APIRouter(tags=["health"])

    @router.get("/health")
    async def health_endpoint(request: Request):
        """Health check endpoint."""
        status = await health.check()
        http_status = 200 if status["status"] != "unhealthy" else 503
        return JSONResponse(content=status, status_code=http_status)

    @router.get("/ready")
    async def readiness_endpoint(request: Request):
        """Kubernetes-style readiness probe."""
        status = await health.check()
        ready = status["status"] == "healthy"
        return JSONResponse(
            content={"ready": ready},
            status_code=200 if ready else 503,
        )

    @router.get("/live")
    async def liveness_endpoint(request: Request):
        """Kubernetes-style liveness probe (always returns 200 if process alive)."""
        return JSONResponse(content={"alive": True}, status_code=200)

    @router.get("/metrics")
    async def metrics_endpoint(request: Request):
        """Prometheus metrics endpoint."""
        from shared.metrics import metrics_response
        from fastapi.responses import Response
        return Response(content=metrics_response(), media_type="text/plain")

    return router
