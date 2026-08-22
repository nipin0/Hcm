"""Data source configuration API — Simple config type.

Provides:
- GET /api/v1/datasource/config — Read all datasource.* config items
- PUT /api/v1/datasource/config — Batch update datasource config

Backward-compatible aliases:
- GET /api/datasource/config
- PUT /api/datasource/config
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

logger = logging.getLogger(__name__)

# ── Config keys (datasource prefix) with defaults

DATASOURCE_CONFIG_DEFAULTS: dict[str, Any] = {
    "primary_source": "mt5",
    "backup_source": "metals-api",
    "auto_failover": True,
    "failover_timeout": 30,
    "data_interval": "m5",
    "cache_ttl": 60,
    "rate_limit_per_min": 60,
    "reconnect_attempts": 5,
    "reconnect_interval": 10,
    "log_level": "info",
}

_DATASOURCE_PREFIX = "datasource."


def _full_key(friendly: str) -> str:
    """Build the full config key from a friendly short name."""
    return f"{_DATASOURCE_PREFIX}{friendly}"


async def _read_config(config_provider: Any) -> dict[str, Any]:
    """Read all datasource.* config values from config_provider."""
    result: dict[str, Any] = {}
    for friendly_key, default in DATASOURCE_CONFIG_DEFAULTS.items():
        full = _full_key(friendly_key)
        raw = await config_provider.get(full)
        if raw is not None:
            try:
                result[friendly_key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                result[friendly_key] = raw
        else:
            result[friendly_key] = default
    return result


async def _write_config(config_provider: Any, body: dict) -> dict:
    """Write a dict of friendly_key → value to config_provider via datasource.* keys."""
    results: list[dict] = []
    success_count = 0
    fail_count = 0

    for friendly_key, value in body.items():
        full = _full_key(friendly_key)
        try:
            ok = await config_provider.set(full, str(value))
            if ok:
                success_count += 1
                results.append({"key": friendly_key, "status": "ok"})
            else:
                fail_count += 1
                results.append({"key": friendly_key, "status": "failed"})
        except Exception as exc:
            fail_count += 1
            results.append({"key": friendly_key, "status": "error", "error": str(exc)})

    return {
        "results": results,
        "success": success_count,
        "failed": fail_count,
        "total": len(body),
    }


# ── Router Factory ─────────────────────────────

def create_datasource_router(
    db_pool: Any = None,
    config_provider: Any = None,
    auth_handler: Any = None,
) -> APIRouter:
    """Create FastAPI router with datasource config endpoints.

    Args:
        db_pool: DatabasePool instance (unused; kept for factory signature consistency).
        config_provider: ConfigProviderV3 instance.
        auth_handler: AuthHandler instance for RBAC.

    Returns:
        APIRouter with datasource config routes.
    """
    router = APIRouter(tags=["datasource"])

    # ── Shared handler implementations ───────────

    async def _get_config(
        request: Request,
        user = Depends(auth_handler.require_auth),
    ):
        """Read all datasource configuration items."""
        if config_provider is None:
            return {"code": "SERVICE_NOT_READY", "data": None, "message": "Config provider not available"}

        try:
            data = await _read_config(config_provider)
            return {"code": 0, "data": data, "message": "ok"}
        except Exception as exc:
            logger.error("Datasource config read failed: %s", exc)
            return {"code": "WB_CFG_001", "data": None, "message": str(exc)}

    async def _put_config(
        body: dict,
        request: Request,
        user = Depends(auth_handler.require_auth),
    ):
        """Batch update datasource configuration."""
        if config_provider is None:
            return {"code": "SERVICE_NOT_READY", "data": None, "message": "Config provider not available"}

        try:
            summary = await _write_config(config_provider, body)
            return {
                "code": 0,
                "data": summary,
                "message": f"Batch update: {summary['success']}/{summary['total']} succeeded",
            }
        except Exception as exc:
            logger.error("Datasource config write failed: %s", exc)
            return {"code": "WB_CFG_002", "data": None, "message": str(exc)}

    # ── V1 routes ────────────────────────────────

    router.add_api_route(
        "/api/v1/datasource/config",
        _get_config,
        methods=["GET"],
        summary="Get datasource configuration",
    )
    router.add_api_route(
        "/api/v1/datasource/config",
        _put_config,
        methods=["PUT"],
        summary="Update datasource configuration",
    )

    # ── Backward-compatible legacy routes ────────

    router.add_api_route(
        "/api/datasource/config",
        _get_config,
        methods=["GET"],
        summary="[Legacy] Get datasource configuration",
        include_in_schema=False,
    )
    router.add_api_route(
        "/api/datasource/config",
        _put_config,
        methods=["PUT"],
        summary="[Legacy] Update datasource configuration",
        include_in_schema=False,
    )

    return router
