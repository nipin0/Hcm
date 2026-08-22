"""Config API — Configuration CRUD endpoints.

Provides:
- GET /api/v1/config — List config with filtering, sorting, pagination
- GET /api/v1/config/{key} — Get single config entry
- PUT /api/v1/config/{key} — Update config value
- PUT /api/v1/config/batch — Batch update config values
- GET /api/v1/config/categories — List config categories
"""

from __future__ import annotations

import json
import logging
import os
from collections import OrderedDict
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

from shared.errors import ErrorCode, HcmError
from shared.models import ApiResponse, ConfigEntry

logger = logging.getLogger(__name__)


# ── Runtime-status: link-stage groups & status helpers ──
# Display semantics (per product requirement):
#   green = live/in-use (referenced & active)
#   white = not enabled (bool switch == false) or legacy/unreferenced
#   red   = abnormal (PG≠Redis conflict, orphan key, or pg-only key)

GROUP_DEFS = [
    ("g1", "行情与指标", 1),
    ("g2", "评分与 Regime", 2),
    ("g3", "实时救场与 AI 门控", 3),
    ("g4", "开仓与平仓", 4),
    ("g5", "AI 提供方", 5),
    ("g6", "风控引擎", 6),
    ("g7", "账户与经纪商", 7),
    ("g8", "系统健康（只读状态）", 8),
]
GROUP_TITLE = {g[0]: g[1] for g in GROUP_DEFS}
GROUP_ORDER = {g[0]: g[2] for g in GROUP_DEFS}

# Keys that are runtime status / market snapshots, not tunable params
STATUS_KEY_PREFIXES = ("watchdog", "latest_kline", "market:latest")

# Sensitive keys that must be masked even if PG catalog misses the flag
SENSITIVE_KEYS = {
    "mt5.password", "mt5.password_hash", "deepseek.api_key",
    "deepseek_degradation_threshold", "jwt_secret", "service_secret",
}

# Redis-only keys with no PG catalog entry yet → assign a link-stage group
REDIS_ONLY_CATEGORY = {
    "bridge.max_entry_slippage_atr_mult": "g4",
    "bridge.max_signal_age_seconds": "g4",
    "close.fallback_atr": "g4",
    "close.max_sl_atr_mult": "g4",
    "close.tp_min_atr_mult": "g4",
    "close.tp_max_atr_mult": "g4",
    "close.trail_start_atr_mult": "g4",
    "scoring.min_score_threshold": "g2",
    "scoring.calibration_enabled": "g2",
    "scoring.calibration_json": "g2",
    "signal_tower.ai_risk_enabled": "g3",
    "signal_tower.zone_trigger_enabled": "g3",
}


def _norm(v: Any) -> str:
    """Normalize a config value for conflict comparison."""
    if v is None:
        return ""
    s = str(v).strip()
    # treat "true"/"1" and "false"/"0" as equal regardless of representation
    if s.lower() in ("true", "1"):
        return "1"
    if s.lower() in ("false", "0"):
        return "0"
    # numeric normalize (drop trailing zeros)
    try:
        f = float(s)
        return repr(f)
    except (ValueError, TypeError):
        return s.lower()


def _is_false_bool(v: Any) -> bool:
    if v is None:
        return False
    s = str(v).strip().lower()
    return s in ("false", "0", "off", "no")


def _classify(key: str, pg_category: Optional[str]) -> str:
    """Map a config key to a link-stage group id."""
    k = key.lower()
    if any(k.startswith(p) for p in STATUS_KEY_PREFIXES):
        return "g8"
    if k.startswith("scoring.live_override") or k.startswith("signal_tower."):
        return "g3"
    if k.startswith("deepseek"):
        return "g5"
    if k.startswith("mt5") or k.startswith("symbol") or k in ("active_symbols",):
        return "g7"
    if k.startswith("risk"):
        return "g6"
    if k.startswith("close") or k.startswith("bridge"):
        return "g4"
    if (
        k.startswith("scoring")
        or k.startswith("regime")
        or k.startswith("cooldown")
        or k.startswith("switch")
        or k.startswith("trend")
        or k.startswith("fade")
        or k.startswith("pretrend")
        or k.startswith("neutral")
        or k.startswith("score")
    ):
        return "g2"
    if k.startswith("datasource") or k.startswith("indicator") or k.startswith("kline"):
        return "g1"
    # fallback by PG category
    cat_map = {
        "datasource": "g1", "indicator": "g1", "kline": "g1",
        "scoring": "g2", "regime": "g2", "cooldown": "g2",
        "close": "g4", "bridge": "g4",
        "deepseek": "g5",
        "risk": "g6",
        "mt5": "g7", "symbol": "g7", "account": "g7",
        "watchdog": "g8", "system": "g8", "health": "g8", "status": "g8",
    }
    if pg_category and pg_category in cat_map:
        return cat_map[pg_category]
    return "g1"


# ── Models ─────────────────────────────────────

class ConfigUpdateRequest(BaseModel):
    """Single config update request."""
    value: str = Field(..., description="New config value")


class BatchConfigUpdate(BaseModel):
    """Batch config update item."""
    config_key: str = Field(..., description="Config key to update")
    value: str = Field(..., description="New value")


class BatchConfigUpdateRequest(BaseModel):
    """Batch config update request."""
    updates: list[BatchConfigUpdate] = Field(..., min_items=1, max_items=100)


class ConfigFilterParams:
    """Parsed config filtering parameters."""

    def __init__(
        self,
        category: Optional[str] = None,
        subcategory: Optional[str] = None,
        scope: Optional[str] = None,
        search: Optional[str] = None,
        symbol: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ):
        self.category = category
        self.subcategory = subcategory
        self.scope = scope
        self.search = search
        self.symbol = symbol
        self.page = max(1, page)
        self.page_size = min(200, max(1, page_size))


# ── Router Factory ─────────────────────────────

def create_config_router(
    db_pool: Any = None,
    config_provider: Any = None,
    auth_handler: Any = None,
    redis_client: Any = None,
) -> APIRouter:
    """Create FastAPI router with config CRUD endpoints.

    Args:
        db_pool: DatabasePool instance.
        config_provider: ConfigProviderV3 instance.
        auth_handler: AuthHandler instance for RBAC.

    Returns:
        APIRouter with config routes.
    """
    router = APIRouter(prefix="/api/v1/config", tags=["config"])

    # ── List Config ─────────────────────────────

    @router.get("")
    async def list_config(
        request: Request,
        category: Optional[str] = Query(None, description="Filter by category"),
        subcategory: Optional[str] = Query(None, description="Filter by subcategory"),
        scope: Optional[str] = Query(None, description="Filter by scope (global/symbol)"),
        search: Optional[str] = Query(None, description="Search in key/label/description"),
        symbol: Optional[str] = Query(None, description="Show symbol-level overrides"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
    ):
        """List config entries with filtering and pagination.

        Supports filtering by category, subcategory, scope, search text,
        and optional symbol-level override display.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            filters = ConfigFilterParams(
                category=category, subcategory=subcategory, scope=scope,
                search=search, symbol=symbol, page=page, page_size=page_size,
            )

            # Build query dynamically
            conditions = ["1=1"]
            params: list[Any] = []
            param_idx = 1

            if filters.category:
                conditions.append(f"category = ${param_idx}")
                params.append(filters.category)
                param_idx += 1

            if filters.subcategory:
                conditions.append(f"subcategory = ${param_idx}")
                params.append(filters.subcategory)
                param_idx += 1

            if filters.scope:
                conditions.append(f"scope = ${param_idx}")
                params.append(filters.scope)
                param_idx += 1

            if filters.search:
                conditions.append(
                    f"(config_key ILIKE ${param_idx} OR label ILIKE ${param_idx} "
                    f"OR description ILIKE ${param_idx})"
                )
                params.append(f"%{filters.search}%")
                param_idx += 1

            where_clause = " AND ".join(conditions)

            # Count total
            count_row = await db_pool.fetchrow(
                f"SELECT COUNT(*) FROM hcm_config.metadata WHERE {where_clause}",
                *params,
            )
            total = count_row[0] if count_row else 0

            # Fetch page
            offset = (filters.page - 1) * filters.page_size
            rows = await db_pool.fetch(
                f"""SELECT config_key, category, subcategory, default_value, current_value,
                           value_type, label, description, ui_control, ui_options,
                           ui_order, scope, is_sensitive, updated_at
                    FROM hcm_config.metadata
                    WHERE {where_clause}
                    ORDER BY category, ui_order, config_key
                    LIMIT ${param_idx} OFFSET ${param_idx + 1}""",
                *params, filters.page_size, offset,
            )

            items = []
            for row in rows:
                item = dict(row)
                # Mask sensitive values
                if item.get("is_sensitive") and item.get("current_value"):
                    item["current_value"] = "***"
                # Convert ui_options from JSON string if needed
                if isinstance(item.get("ui_options"), str):
                    try:
                        item["ui_options"] = json.loads(item["ui_options"])
                    except (json.JSONDecodeError, TypeError):
                        item["ui_options"] = None
                # Format datetime
                if item.get("updated_at"):
                    item["updated_at"] = item["updated_at"].isoformat()
                items.append(item)

            return {
                "code": 0,
                "data": {
                    "items": items,
                    "total": total,
                    "page": filters.page,
                    "page_size": filters.page_size,
                },
                "message": "ok",
            }

        except Exception as exc:
            logger.error("Config list failed: %s", exc)
            return {"code": "WB_CFG_001", "data": None, "message": str(exc)}

    # ── Get All Config (Redis hash dump) ────────
    # MUST be before /{config_key:path} catch-all

    @router.get("/all")
    async def get_all_config():
        """Return all hcm:config:v2 key-value pairs from Redis."""
        if redis_client is None or not redis_client.is_initialized:
            return {"code": "SYS_REDIS_001", "data": None, "message": "Redis not available"}
        try:
            raw = await redis_client.hgetall("hcm:config:v2")
            return {"code": 0, "data": {k: v for k, v in raw.items()}}
        except Exception as exc:
            logger.error("Config all read failed: %s", exc)
            return {"code": "WB_CFG_001", "data": None, "message": str(exc)}

    # ── Runtime Status (grouped, status-colored, masked) ──
    # Replaces the hardcoded dashboard "运行时参数" panel with a
    # data-driven view: grouped by link-stage, color = lifecycle status.

    @router.get("/runtime-status")
    async def runtime_status():
        """Return all config keys grouped by link-stage with status flags.

        - value: Redis runtime value (masked if sensitive)
        - pg_value: PG catalog current_value
        - in_redis / in_pg: presence in each store
        - conflict: PG current_value != Redis value
        - status: green=live/in-use, white=disabled/unreferenced, red=abnormal
        """
        try:
            raw = {}
            if redis_client is not None and redis_client.is_initialized:
                raw = await redis_client.hgetall("hcm:config:v2") or {}

            pg_rows = []
            if db_pool is not None and db_pool.is_initialized:
                pg_rows = await db_pool.fetch(
                    """SELECT config_key, category, subcategory, default_value,
                              current_value, value_type, label, description,
                              scope, is_sensitive
                       FROM hcm_config.metadata"""
                )
            pg = {r["config_key"]: dict(r) for r in pg_rows}

            sensitive_set = set(SENSITIVE_KEYS)
            for k, v in pg.items():
                if v.get("is_sensitive"):
                    sensitive_set.add(k)

            all_keys = list(raw.keys() | pg.keys())
            groups: "OrderedDict[str, dict]" = OrderedDict()

            for key in all_keys:
                rv = raw.get(key)
                pv = pg.get(key, {}).get("current_value") if key in pg else None
                in_redis = key in raw
                in_pg = key in pg
                conflict = bool(
                    in_redis and in_pg
                    and rv is not None and pv is not None
                    and _norm(rv) != _norm(pv)
                )
                is_sensitive = key in sensitive_set or bool(
                    pg.get(key, {}).get("is_sensitive")
                )
                show_val = "***" if is_sensitive else (rv if rv is not None else pv)

                if conflict:
                    status = "red"
                elif in_redis and not in_pg:
                    status = "red"          # active orphan: not in catalog
                elif in_pg and not in_redis:
                    status = "red"          # catalog-only: never loaded
                elif _is_false_bool(show_val):
                    status = "white"
                else:
                    status = "green"

                gid = _classify(key, pg.get(key, {}).get("category"))
                if gid not in groups:
                    groups[gid] = {
                        "id": gid,
                        "title": GROUP_TITLE[gid],
                        "order": GROUP_ORDER[gid],
                        "params": [],
                    }
                groups[gid]["params"].append({
                    "key": key,
                    "value": show_val,
                    "pg_value": pv,
                    "in_redis": in_redis,
                    "in_pg": in_pg,
                    "conflict": conflict,
                    "is_sensitive": is_sensitive,
                    "status": status,
                    "label": (pg.get(key, {}).get("label") or key),
                    "description": (pg.get(key, {}).get("description") or ""),
                    "category": (pg.get(key, {}).get("category")
                                 or REDIS_ONLY_CATEGORY.get(key, "")),
                })

            result = sorted(groups.values(), key=lambda g: g["order"])
            counts = {"green": 0, "white": 0, "red": 0}
            for g in result:
                g["params"].sort(key=lambda p: p["key"])
                for p in g["params"]:
                    counts[p["status"]] += 1

            return {
                "code": 0,
                "data": {
                    "groups": result,
                    "total": len(all_keys),
                    "counts": counts,
                },
                "message": "ok",
            }
        except Exception as exc:
            logger.error("Runtime status failed: %s", exc)
            return {"code": "WB_CFG_001", "data": None, "message": str(exc)}

    @router.get("/runtime-params")
    async def runtime_params_page():
        """Serve the self-contained runtime-params dashboard panel (HTML)."""
        tpl = os.path.join(os.path.dirname(__file__), "..", "templates", "runtime_params.html")
        try:
            with open(tpl, "r", encoding="utf-8") as fh:
                html = fh.read()
            return HTMLResponse(content=html, media_type="text/html")
        except FileNotFoundError:
            return HTMLResponse(
                content="<h1>runtime_params.html not found</h1>", status_code=404
            )

    # ── Batch Write Config (PG+Redis dual-write via config_provider) ──

    @router.post("/batch")
    async def batch_set_config(body: dict):
        """Batch write config keys: {"key1": "val1", "key2": "val2"}.
        Writes to PG (metadata table) + Redis (hcm:config:v2) via config_provider.
        """
        if config_provider is None:
            return {"code": "SERVICE_NOT_READY", "data": None, "message": "Config provider not available"}
        ok = fail = 0
        for k, v in body.items():
            try:
                if await config_provider.set(k, str(v)):
                    ok += 1
                else:
                    fail += 1
            except Exception as exc:
                logger.error("Config batch write failed for key=%s: %s", k, exc)
                fail += 1
        return {"code": 0, "message": f"Updated {ok} keys (failed {fail})"}

    # ── Get Single Config ───────────────────────

    @router.get("/{config_key:path}")
    async def get_config(config_key: str):
        """Get a single config entry by key.

        Args:
            config_key: Config key (supports path-like keys with dots).
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            row = await db_pool.fetchrow(
                """SELECT config_key, category, subcategory, default_value, current_value,
                          value_type, label, description, ui_control, ui_options,
                          ui_order, scope, is_sensitive, created_at, updated_at
                   FROM hcm_config.metadata WHERE config_key = $1""",
                config_key,
            )

            if row is None:
                return {
                    "code": "CFG_LOAD_002",
                    "data": None,
                    "message": f"Config key not found: {config_key}",
                }

            item = dict(row)
            if item.get("is_sensitive") and item.get("current_value"):
                item["current_value"] = "***"
            if isinstance(item.get("ui_options"), str):
                try:
                    item["ui_options"] = json.loads(item["ui_options"])
                except (json.JSONDecodeError, TypeError):
                    item["ui_options"] = None
            if item.get("created_at"):
                item["created_at"] = item["created_at"].isoformat()
            if item.get("updated_at"):
                item["updated_at"] = item["updated_at"].isoformat()

            return {"code": 0, "data": item, "message": "ok"}

        except Exception as exc:
            logger.error("Config get failed for key=%s: %s", config_key, exc)
            return {"code": "WB_CFG_001", "data": None, "message": str(exc)}

    # ── Update Config ───────────────────────────

    @router.put("/{config_key:path}")
    async def update_config(config_key: str, body: ConfigUpdateRequest):
        """Update a single config value.

        Writes to PG (source of truth), updates Redis cache,
        and broadcasts invalidation.
        """
        if config_provider is None:
            return {"code": "SERVICE_NOT_READY", "data": None, "message": "Config provider not available"}

        try:
            success = await config_provider.set(config_key, body.value)
            if success:
                logger.info("Config updated: key=%s, value=%s", config_key, body.value[:50])
                return {"code": 0, "data": {"config_key": config_key, "value": body.value}, "message": "ok"}
            else:
                return {"code": "WB_CFG_002", "data": None, "message": "Config write failed"}

        except Exception as exc:
            logger.error("Config update failed for key=%s: %s", config_key, exc)
            return {"code": "WB_CFG_002", "data": None, "message": str(exc)}

    # ── Batch Update ────────────────────────────

    @router.put("/batch")
    async def batch_update_config(body: BatchConfigUpdateRequest):
        """Batch update multiple config values.

        Args:
            body: List of config updates.
        """
        if config_provider is None:
            return {"code": "SERVICE_NOT_READY", "data": None, "message": "Config provider not available"}

        results: list[dict] = []
        success_count = 0
        fail_count = 0

        for update in body.updates:
            try:
                ok = await config_provider.set(update.config_key, update.value)
                if ok:
                    success_count += 1
                    results.append({"config_key": update.config_key, "status": "ok"})
                else:
                    fail_count += 1
                    results.append({"config_key": update.config_key, "status": "failed"})
            except Exception as exc:
                fail_count += 1
                results.append({
                    "config_key": update.config_key,
                    "status": "error",
                    "error": str(exc),
                })

        return {
            "code": 0,
            "data": {
                "results": results,
                "success": success_count,
                "failed": fail_count,
                "total": len(body.updates),
            },
            "message": f"Batch update: {success_count}/{len(body.updates)} succeeded",
        }

    # ── Categories ──────────────────────────────

    @router.get("/categories/list")
    async def list_categories():
        """List distinct config categories with counts."""
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            rows = await db_pool.fetch(
                """SELECT category, COUNT(*) as count
                   FROM hcm_config.metadata
                   GROUP BY category
                   ORDER BY category"""
            )
            categories = [{"category": r["category"], "count": r["count"]} for r in rows]
            return {"code": 0, "data": categories, "message": "ok"}

        except Exception as exc:
            logger.error("Config categories list failed: %s", exc)
            return {"code": "WB_CFG_001", "data": None, "message": str(exc)}

    return router
