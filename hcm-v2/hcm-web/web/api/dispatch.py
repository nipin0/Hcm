"""Dispatch configuration API — Simple config type.

Provides:
- GET /api/v1/dispatch/config — Read all dispatch.* config items
- PUT /api/v1/dispatch/config — Batch update dispatch config

Backward-compatible aliases:
- GET /api/dispatch/config
- PUT /api/dispatch/config
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

logger = logging.getLogger(__name__)

# ── Config keys (dispatch prefix) with defaults ──

DISPATCH_CONFIG_DEFAULTS: dict[str, Any] = {
    "dispatch_method": "auto",
    "target_platform": "mt5",
    "max_retry": 3,
    "retry_interval": 5,
    "order_timeout": 30,
    "enable_partial_fill": False,
    "slippage_tolerance": 5,
    "dispatch_log_level": "info",
}

# Friendly key → full config key mapping
_DISPATCH_PREFIX = "dispatch."


def _full_key(friendly: str) -> str:
    """Build the full config key from a friendly short name."""
    return f"{_DISPATCH_PREFIX}{friendly}"


def _friendly(full: str) -> str:
    """Strip the dispatch. prefix from a config key."""
    return full[len(_DISPATCH_PREFIX):] if full.startswith(_DISPATCH_PREFIX) else full


async def _read_config(config_provider: Any) -> dict[str, Any]:
    """Read all dispatch.* config values from config_provider.

    Returns a dict of friendly_key → parsed_value.
    If a key is missing, the default value from DISPATCH_CONFIG_DEFAULTS is used.
    """
    result: dict[str, Any] = {}
    for friendly_key, default in DISPATCH_CONFIG_DEFAULTS.items():
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
    """Write a dict of friendly_key → value to config_provider.

    Returns a summary dict with per-key results.
    """
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

def create_dispatch_router(
    db_pool: Any = None,
    config_provider: Any = None,
    auth_handler: Any = None,
) -> APIRouter:
    """Create FastAPI router with dispatch config endpoints.

    Args:
        db_pool: DatabasePool instance (unused; kept for factory signature consistency).
        config_provider: ConfigProviderV3 instance.
        auth_handler: AuthHandler instance for RBAC.

    Returns:
        APIRouter with dispatch config routes.
    """
    router = APIRouter(tags=["dispatch"])

    # ── Shared handler implementations ───────────

    async def _get_config(
        request: Request,
        user = Depends(auth_handler.require_auth),
    ):
        """Read all dispatch configuration items."""
        if config_provider is None:
            return {"code": "SERVICE_NOT_READY", "data": None, "message": "Config provider not available"}

        try:
            data = await _read_config(config_provider)
            return {"code": 0, "data": data, "message": "ok"}
        except Exception as exc:
            logger.error("Dispatch config read failed: %s", exc)
            return {"code": "WB_CFG_001", "data": None, "message": str(exc)}

    async def _put_config(
        body: dict,
        request: Request,
        user = Depends(auth_handler.require_auth),
    ):
        """Batch update dispatch configuration."""
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
            logger.error("Dispatch config write failed: %s", exc)
            return {"code": "WB_CFG_002", "data": None, "message": str(exc)}

    # ── V1 routes ────────────────────────────────

    router.add_api_route(
        "/api/v1/dispatch/config",
        _get_config,
        methods=["GET"],
        summary="Get dispatch configuration",
    )
    router.add_api_route(
        "/api/v1/dispatch/config",
        _put_config,
        methods=["PUT"],
        summary="Update dispatch configuration",
    )

    # ── Backward-compatible legacy routes ────────

    router.add_api_route(
        "/api/dispatch/config",
        _get_config,
        methods=["GET"],
        summary="[Legacy] Get dispatch configuration",
        include_in_schema=False,
    )
    router.add_api_route(
        "/api/dispatch/config",
        _put_config,
        methods=["PUT"],
        summary="[Legacy] Update dispatch configuration",
        include_in_schema=False,
    )

    return router
