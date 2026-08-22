"""Inference Engine Rules API — Full CRUD type.

Provides:
- GET    /api/v1/engine/rules         — List rules with filtering
- POST   /api/v1/engine/rules         — Create a new rule
- GET    /api/v1/engine/rules/{id}    — Get rule detail
- PUT    /api/v1/engine/rules/{id}    — Update rule (incl. enable/disable)
- DELETE /api/v1/engine/rules/{id}    — Delete rule

Backward-compatible aliases:
- GET    /api/engine/rules
- POST   /api/engine/rules
- GET    /api/engine/rules/{id}
- PUT    /api/engine/rules/{id}
- DELETE /api/engine/rules/{id}

Database bootstrap: Creates hcm_engine schema and rules table on first access
if they don't already exist.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ── DDL for auto-bootstrap ──────────────────────

_SCHEMA_DDL = "CREATE SCHEMA IF NOT EXISTS hcm_engine"

_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS hcm_engine.rules (
    rule_id      SERIAL PRIMARY KEY,
    name         VARCHAR(100) NOT NULL,
    type         VARCHAR(30) NOT NULL,
    priority     INT DEFAULT 0,
    enabled      BOOLEAN DEFAULT true,
    description  TEXT DEFAULT '',
    config       JSONB DEFAULT '{}',
    created_at   TIMESTAMPTZ DEFAULT now(),
    updated_at   TIMESTAMPTZ DEFAULT now()
)
"""

# Track whether DDL has been executed (module-level, per process)
_ddl_executed: bool = False


# ── Pydantic models ─────────────────────────────

class EngineRuleCreate(BaseModel):
    """Request model for creating an inference engine rule."""
    name: str = Field(..., min_length=1, max_length=100, description="Rule name")
    type: str = Field(..., description="Rule type: signal_filter | risk_check | dispatch_rule")
    priority: int = Field(0, description="Execution priority (higher = first)")
    enabled: bool = Field(True, description="Whether the rule is enabled")
    description: str = Field("", description="Human-readable description")
    config: dict = Field(default_factory=dict, description="Rule configuration JSON")


class EngineRuleUpdate(BaseModel):
    """Request model for updating an inference engine rule.

    All fields are optional; only provided fields are updated.
    """
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    type: Optional[str] = None
    priority: Optional[int] = None
    enabled: Optional[bool] = None
    description: Optional[str] = None
    config: Optional[dict] = None


# ── Helpers ─────────────────────────────────────

def _row_to_dict(row: Any) -> dict:
    """Convert a database row to a dict with proper serialization."""
    item = dict(row)
    # Ensure config is a dict (may come as JSON string from some drivers)
    if isinstance(item.get("config"), str):
        try:
            item["config"] = json.loads(item["config"])
        except (json.JSONDecodeError, TypeError):
            item["config"] = {}
    # Serialize datetimes
    for dt_field in ("created_at", "updated_at"):
        if item.get(dt_field):
            item[dt_field] = item[dt_field].isoformat()
    return item


async def _ensure_schema(db_pool: Any) -> None:
    """Create hcm_engine schema and rules table if they don't exist.

    Idempotent — safe to call on every request; only executes DDL once per process.
    """
    global _ddl_executed
    if _ddl_executed:
        return
    if db_pool is None or not db_pool.is_initialized:
        return

    try:
        await db_pool.execute(_SCHEMA_DDL)
        await db_pool.execute(_TABLE_DDL)
        _ddl_executed = True
        logger.info("Engine DDL bootstrap completed: hcm_engine.rules is ready")
    except Exception as exc:
        logger.error("Engine DDL bootstrap failed: %s", exc)
        # Don't set _ddl_executed so we retry next time


# ── Router Factory ─────────────────────────────

def create_engine_router(
    db_pool: Any = None,
    config_provider: Any = None,
    auth_handler: Any = None,
) -> APIRouter:
    """Create FastAPI router with inference engine rule CRUD endpoints.

    Args:
        db_pool: DatabasePool instance for rule storage.
        config_provider: ConfigProviderV3 instance (unused; kept for factory signature consistency).
        auth_handler: AuthHandler instance for RBAC.

    Returns:
        APIRouter with engine rule routes.
    """
    router = APIRouter(tags=["engine"])

    # ── List Rules ───────────────────────────────

    async def _list_rules(
        request: Request,
        user = Depends(auth_handler.require_auth),
        type: Optional[str] = Query(None, description="Filter by rule type"),
        enabled: Optional[bool] = Query(None, description="Filter by enabled status"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
    ):
        """List inference engine rules with optional filtering and pagination."""
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        await _ensure_schema(db_pool)

        try:
            conditions: list[str] = ["1=1"]
            params: list[Any] = []
            param_idx = 1

            if type is not None:
                conditions.append(f"type = ${param_idx}")
                params.append(type)
                param_idx += 1

            if enabled is not None:
                conditions.append(f"enabled = ${param_idx}")
                params.append(enabled)
                param_idx += 1

            where_clause = " AND ".join(conditions)

            # Count total
            count_row = await db_pool.fetchrow(
                f"SELECT COUNT(*) FROM hcm_engine.rules WHERE {where_clause}",
                *params,
            )
            total = count_row[0] if count_row else 0

            # Fetch page
            offset = (page - 1) * page_size
            rows = await db_pool.fetch(
                f"""SELECT rule_id, name, type, priority, enabled, description, config,
                           created_at, updated_at
                    FROM hcm_engine.rules
                    WHERE {where_clause}
                    ORDER BY priority DESC, rule_id ASC
                    LIMIT ${param_idx} OFFSET ${param_idx + 1}""",
                *params, page_size, offset,
            )

            items = [_row_to_dict(r) for r in rows]

            return {
                "code": 0,
                "data": {
                    "items": items,
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                },
                "message": "ok",
            }

        except Exception as exc:
            logger.error("Engine rules list failed: %s", exc)
            return {"code": "WB_CFG_001", "data": None, "message": str(exc)}

    # ── Create Rule ──────────────────────────────

    async def _create_rule(
        body: EngineRuleCreate,
        request: Request,
        user = Depends(auth_handler.require_auth),
    ):
        """Create a new inference engine rule."""
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        await _ensure_schema(db_pool)

        try:
            config_json = json.dumps(body.config) if body.config else "{}"
            row = await db_pool.fetchrow(
                """INSERT INTO hcm_engine.rules
                       (name, type, priority, enabled, description, config)
                   VALUES ($1, $2, $3, $4, $5, $6::jsonb)
                   RETURNING rule_id, name, type, priority, enabled, description,
                             config, created_at, updated_at""",
                body.name, body.type, body.priority, body.enabled, body.description, config_json,
            )

            item = _row_to_dict(row)
            logger.info("Engine rule created: rule_id=%s, name=%s", item["rule_id"], body.name)

            return {"code": 0, "data": item, "message": "ok"}

        except Exception as exc:
            logger.error("Engine rule create failed: %s", exc)
            return {"code": "WB_CFG_002", "data": None, "message": str(exc)}

    # ── Get Rule Detail ──────────────────────────

    async def _get_rule(
        rule_id: int,
        request: Request,
        user = Depends(auth_handler.require_auth),
    ):
        """Get a single inference engine rule by ID."""
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        await _ensure_schema(db_pool)

        try:
            row = await db_pool.fetchrow(
                """SELECT rule_id, name, type, priority, enabled, description, config,
                          created_at, updated_at
                   FROM hcm_engine.rules WHERE rule_id = $1""",
                rule_id,
            )

            if row is None:
                return {
                    "code": "CFG_LOAD_002",
                    "data": None,
                    "message": f"Rule not found: {rule_id}",
                }

            return {"code": 0, "data": _row_to_dict(row), "message": "ok"}

        except Exception as exc:
            logger.error("Engine rule get failed for rule_id=%s: %s", rule_id, exc)
            return {"code": "WB_CFG_001", "data": None, "message": str(exc)}

    # ── Update Rule ──────────────────────────────

    async def _update_rule(
        rule_id: int,
        body: EngineRuleUpdate,
        request: Request,
        user = Depends(auth_handler.require_auth),
    ):
        """Update an inference engine rule (partial update).

        Only provided fields are modified.  Set enabled=false to disable.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        await _ensure_schema(db_pool)

        try:
            # Build dynamic SET clause
            set_parts: list[str] = []
            params: list[Any] = []
            param_idx = 1

            updates = body.model_dump(exclude_unset=True)
            if not updates:
                return {"code": 0, "data": None, "message": "No fields to update"}

            for field, value in updates.items():
                if field == "config" and value is not None:
                    set_parts.append(f"config = ${param_idx}::jsonb")
                    params.append(json.dumps(value))
                else:
                    set_parts.append(f"{field} = ${param_idx}")
                    params.append(value)
                param_idx += 1

            # Always bump updated_at
            set_parts.append("updated_at = now()")

            params.append(rule_id)
            where_idx = param_idx

            set_clause = ", ".join(set_parts)

            row = await db_pool.fetchrow(
                f"""UPDATE hcm_engine.rules
                    SET {set_clause}
                    WHERE rule_id = ${where_idx}
                    RETURNING rule_id, name, type, priority, enabled, description,
                              config, created_at, updated_at""",
                *params,
            )

            if row is None:
                return {
                    "code": "CFG_LOAD_002",
                    "data": None,
                    "message": f"Rule not found: {rule_id}",
                }

            logger.info("Engine rule updated: rule_id=%s", rule_id)

            return {"code": 0, "data": _row_to_dict(row), "message": "ok"}

        except Exception as exc:
            logger.error("Engine rule update failed for rule_id=%s: %s", rule_id, exc)
            return {"code": "WB_CFG_002", "data": None, "message": str(exc)}

    # ── Delete Rule ──────────────────────────────

    async def _delete_rule(
        rule_id: int,
        request: Request,
        user = Depends(auth_handler.require_auth),
    ):
        """Delete an inference engine rule by ID."""
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        await _ensure_schema(db_pool)

        try:
            result = await db_pool.execute(
                "DELETE FROM hcm_engine.rules WHERE rule_id = $1",
                rule_id,
            )

            # asyncpg execute returns a string like "DELETE 1" or "DELETE 0"
            deleted = result and "DELETE 1" in str(result) if isinstance(result, str) else bool(result)

            if not deleted:
                return {
                    "code": "CFG_LOAD_002",
                    "data": None,
                    "message": f"Rule not found: {rule_id}",
                }

            logger.info("Engine rule deleted: rule_id=%s", rule_id)

            return {"code": 0, "data": {"rule_id": rule_id, "deleted": True}, "message": "ok"}

        except Exception as exc:
            logger.error("Engine rule delete failed for rule_id=%s: %s", rule_id, exc)
            return {"code": "WB_CFG_002", "data": None, "message": str(exc)}

    # ── Register V1 routes ───────────────────────

    router.add_api_route(
        "/api/v1/engine/rules",
        _list_rules,
        methods=["GET"],
        summary="List engine rules",
    )
    router.add_api_route(
        "/api/v1/engine/rules",
        _create_rule,
        methods=["POST"],
        summary="Create engine rule",
    )
    router.add_api_route(
        "/api/v1/engine/rules/{rule_id}",
        _get_rule,
        methods=["GET"],
        summary="Get engine rule detail",
    )
    router.add_api_route(
        "/api/v1/engine/rules/{rule_id}",
        _update_rule,
        methods=["PUT"],
        summary="Update engine rule",
    )
    router.add_api_route(
        "/api/v1/engine/rules/{rule_id}",
        _delete_rule,
        methods=["DELETE"],
        summary="Delete engine rule",
    )

    # ── Register legacy routes ───────────────────

    router.add_api_route(
        "/api/engine/rules",
        _list_rules,
        methods=["GET"],
        summary="[Legacy] List engine rules",
        include_in_schema=False,
    )
    router.add_api_route(
        "/api/engine/rules",
        _create_rule,
        methods=["POST"],
        summary="[Legacy] Create engine rule",
        include_in_schema=False,
    )
    router.add_api_route(
        "/api/engine/rules/{rule_id}",
        _get_rule,
        methods=["GET"],
        summary="[Legacy] Get engine rule detail",
        include_in_schema=False,
    )
    router.add_api_route(
        "/api/engine/rules/{rule_id}",
        _update_rule,
        methods=["PUT"],
        summary="[Legacy] Update engine rule",
        include_in_schema=False,
    )
    router.add_api_route(
        "/api/engine/rules/{rule_id}",
        _delete_rule,
        methods=["DELETE"],
        summary="[Legacy] Delete engine rule",
        include_in_schema=False,
    )

    return router
