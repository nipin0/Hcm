"""Copy API — Copy trading relationship and symbol mapping CRUD endpoints.

Provides:
- GET/POST   /api/v1/copy/relationships           — List/Create copy relationships
- GET/PUT/DELETE /api/v1/copy/relationships/{relationship_id} — Detail/Update/Delete
- GET        /api/v1/copy/relationships/{relationship_id}/logs — Trade logs (paginated)
- GET/POST   /api/v1/copy/symbol-mappings          — List/Create symbol mappings
- PUT/DELETE /api/v1/copy/symbol-mappings/{mapping_id} — Update/Delete
- PUT        /api/v1/copy/symbol-mappings/{mapping_id}/toggle — Enable/Disable

Legacy backwards-compatibility aliases (same router):
- GET  /api/copy/config — Returns relationships list + symbol mappings
- PUT  /api/copy/config — Parse legacy frontend fields and write
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator

from shared.models import (
    CopyRelationshipResponse,
    PaginatedResponse,
    SymbolMappingResponse,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════
#  Pydantic Request Models
# ═══════════════════════════════════════════════

# 建立跟单关系时，前端数字输入可能以空串 '' 提交，Pydantic 解析 float/int 会因空串抛 422。
# 校验前统一把空串 / None 回退到默认值 / None（用模块级常量，避免被 Pydantic 当作私有属性）。
_COPY_REL_DEFAULTS: dict = {
    "lot_multiplier": 1.0,
    "min_lot": 0.01,
    "max_lot": 5.0,
    "max_positions": 10,
    "max_daily_loss": 0.0,
    "max_consecutive_losses": 3,
    "retry_max": 3,
}
_COPY_REL_NUMERIC = {"max_slippage_pips", "max_execution_delay_ms"}


class CopyRelationshipCreate(BaseModel):
    """Request model for creating a copy trading relationship."""
    master_account_id: int
    copy_account_id: int
    status: str = "stopped"                    # running / stopped / paused
    lot_mode: str = "multiplier"               # multiplier / fixed / balance_ratio
    lot_multiplier: float = 1.0
    min_lot: float = 0.01
    max_lot: float = 5.0
    max_positions: int = 10
    max_daily_loss: float = 0.0
    max_consecutive_losses: int = 3
    direction_mode: str = "FORWARD"             # FORWARD / REVERSE / BOTH
    copy_sl: bool = True
    copy_tp: bool = True
    sync_mode: str = "pubsub"                  # pubsub / poll
    retry_on_failure: bool = True
    retry_max: int = 3
    max_slippage_pips: Optional[float] = Field(None, ge=0, description="滑点保护（点数），超过取消跟单")
    max_execution_delay_ms: Optional[int] = Field(None, ge=0, description="开仓最大延迟（毫秒），超时不执行")

    # 校验前把空串 / None 回退到默认值，杜绝建立跟单关系 422。
    @model_validator(mode="before")
    @classmethod
    def _coerce_empty_strings(cls, data):
        if isinstance(data, dict):
            for k, v in list(data.items()):
                if v == "" or v is None:
                    if k in _COPY_REL_DEFAULTS:
                        data[k] = _COPY_REL_DEFAULTS[k]
                    elif k in _COPY_REL_NUMERIC:
                        data[k] = None
        return data


class CopyRelationshipUpdate(BaseModel):
    """Request model for updating a copy trading relationship.

    All fields are optional — only provided fields are updated.
    """
    status: Optional[str] = None
    lot_mode: Optional[str] = None
    lot_multiplier: Optional[float] = None
    min_lot: Optional[float] = None
    max_lot: Optional[float] = None
    max_positions: Optional[int] = None
    max_daily_loss: Optional[float] = None
    max_consecutive_losses: Optional[int] = None
    direction_mode: Optional[str] = None
    copy_sl: Optional[bool] = None
    copy_tp: Optional[bool] = None
    sync_mode: Optional[str] = None
    retry_on_failure: Optional[bool] = None
    retry_max: Optional[int] = None
    max_slippage_pips: Optional[float] = Field(None, ge=0, description="滑点保护（点数），超过取消跟单")
    max_execution_delay_ms: Optional[int] = Field(None, ge=0, description="开仓最大延迟（毫秒），超时不执行")


class SymbolMappingCreate(BaseModel):
    """Request model for creating a cross-broker symbol mapping."""
    master_broker: str
    master_symbol: str
    follower_broker: str = ""
    follower_symbol: str
    match_mode: str = "exact"                  # exact / prefix / suffix / manual
    match_priority: int = 0
    is_active: bool = False                    # default disabled (safety constraint)


class SymbolMappingUpdate(BaseModel):
    """Request model for updating a symbol mapping.

    All fields are optional — only provided fields are updated.
    """
    master_broker: Optional[str] = None
    follower_broker: Optional[str] = None
    follower_symbol: Optional[str] = None
    match_mode: Optional[str] = None
    match_priority: Optional[int] = None


# ═══════════════════════════════════════════════
#  Legacy Config Model (PUT /api/copy/config)
# ═══════════════════════════════════════════════

class LegacyCopyConfigRequest(BaseModel):
    """Legacy PUT /api/copy/config request body.

    Parses the old frontend flat-object format and maps fields to the new
    relationships / symbol-mappings model.
    """
    master_account_id: Optional[int] = None
    copy_account_id: Optional[int] = None
    status: Optional[str] = None
    lot_mode: Optional[str] = None
    lot_multiplier: Optional[float] = None
    min_lot: Optional[float] = None
    max_lot: Optional[float] = None
    max_positions: Optional[int] = None
    max_daily_loss: Optional[float] = None
    max_consecutive_losses: Optional[int] = None
    direction_mode: Optional[str] = None
    copy_sl: Optional[bool] = None
    copy_tp: Optional[bool] = None
    sync_mode: Optional[str] = None
    # Legacy field aliases accommodated via model_extra="allow"
    model_config = {"extra": "allow"}


# ═══════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════

def _row_to_relationship(row: dict) -> dict:
    """Convert a DB row dict to a serializable copy-relationship dict.

    Formats datetimes to ISO strings so the response is JSON-safe.
    """
    item = dict(row)
    for dt_field in ("created_at", "updated_at"):
        if item.get(dt_field):
            item[dt_field] = item[dt_field].isoformat()
    return item


def _row_to_mapping(row: dict) -> dict:
    """Convert a DB row dict to a serializable symbol-mapping dict."""
    item = dict(row)
    for dt_field in ("created_at", "updated_at"):
        if item.get(dt_field):
            item[dt_field] = item[dt_field].isoformat()
    return item


def _row_to_trade_log(row: dict) -> dict:
    """Convert a DB row dict to a serializable trade-log dict."""
    item = dict(row)
    for dt_field in ("created_at",):
        if item.get(dt_field):
            item[dt_field] = item[dt_field].isoformat()
    return item


def _clamp_page(page: int, page_size: int) -> tuple[int, int]:
    """Clamp pagination parameters to safe bounds."""
    p = max(1, page)
    ps = min(200, max(1, page_size))
    return p, ps


# ═══════════════════════════════════════════════
#  Router Factory
# ═══════════════════════════════════════════════

def create_copy_router(
    db_pool: Any = None,
    config_provider: Any = None,
    auth_handler: Any = None,
) -> APIRouter:
    """Create FastAPI router with copy-trading CRUD endpoints.

    Args:
        db_pool: DatabasePool instance (asyncpg pool wrapper).
        config_provider: ConfigProviderV3 instance for runtime config.
        auth_handler: AuthHandler instance for JWT RBAC.

    Returns:
        Tuple of (v1_router, legacy_router).
        v1_router has prefix /api/v1/copy.
        legacy_router has prefix /api/copy for backwards compatibility.
    """
    router = APIRouter(prefix="/api/v1/copy", tags=["copy"])

    # ── helper: DB guard ───────────────────────

    def _db_ok() -> bool:
        return db_pool is not None and db_pool.is_initialized

    # ═══════════════════════════════════════════
    #  Relationships — List
    # ═══════════════════════════════════════════

    @router.get("/relationships")
    async def list_relationships(
        request: Request,
        user=Depends(auth_handler.require_auth),
        status: Optional[str] = Query(None, description="Filter by status (running/stopped/paused)"),
        master_id: Optional[int] = Query(None, description="Filter by master account ID"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
    ):
        """List copy trading relationships with optional filters.

        Query parameters:
        - status:  Filter by relationship status.
        - master_id: Filter by master account ID.
        - page / page_size: Pagination.
        """
        if not _db_ok():
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            page, page_size = _clamp_page(page, page_size)
            conditions: list[str] = ["1=1"]
            params: list[Any] = []
            idx = 1

            if status is not None:
                conditions.append(f"status = ${idx}")
                params.append(status)
                idx += 1

            if master_id is not None:
                conditions.append(f"master_account_id = ${idx}")
                params.append(master_id)
                idx += 1

            where = " AND ".join(conditions)

            # Count
            count_row = await db_pool.fetchrow(
                f"SELECT COUNT(*) FROM hcm_copy.relationships WHERE {where}",
                *params,
            )
            total = count_row[0] if count_row else 0

            # Fetch
            offset = (page - 1) * page_size
            rows = await db_pool.fetch(
                f"""SELECT relationship_id, master_account_id, copy_account_id,
                           status, lot_mode, lot_multiplier, min_lot, max_lot,
                           max_positions, max_daily_loss, max_consecutive_losses,
                           direction_mode, copy_sl, copy_tp, sync_mode,
                           created_at, updated_at
                    FROM hcm_copy.relationships
                    WHERE {where}
                    ORDER BY relationship_id DESC
                    LIMIT ${idx} OFFSET ${idx + 1}""",
                *params, page_size, offset,
            )

            items = [_row_to_relationship(dict(r)) for r in rows]

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
            logger.error("list_relationships failed: %s", exc)
            return {"code": "WB_CFG_001", "data": None, "message": str(exc)}

    # ═══════════════════════════════════════════
    #  Relationships — Create
    # ═══════════════════════════════════════════

    @router.post("/relationships")
    async def create_relationship(
        request: Request,
        body: CopyRelationshipCreate,
        user=Depends(auth_handler.require_auth),
    ):
        """Create a new copy trading relationship.

        Validates that both master and follower accounts exist in hcm_broker.accounts
        before inserting.
        """
        if not _db_ok():
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            # ── Validate both accounts exist ────
            account_row = await db_pool.fetchrow(
                """SELECT COUNT(*) as cnt
                   FROM hcm_broker.accounts
                   WHERE account_id IN ($1, $2) AND is_active = true""",
                body.master_account_id,
                body.copy_account_id,
            )
            if account_row is None or account_row["cnt"] < 2:
                return {
                    "code": "COPY_002",
                    "data": None,
                    "message": "Master or follower account does not exist or is inactive",
                }

            # ── Check for duplicate ─────────────
            dup = await db_pool.fetchrow(
                """SELECT relationship_id FROM hcm_copy.relationships
                   WHERE master_account_id = $1 AND copy_account_id = $2""",
                body.master_account_id,
                body.copy_account_id,
            )
            if dup is not None:
                return {
                    "code": "COPY_001",
                    "data": None,
                    "message": "Copy relationship already exists for this master/follower pair",
                }

            # ── Insert ──────────────────────────
            row = await db_pool.fetchrow(
                """INSERT INTO hcm_copy.relationships
                      (master_account_id, copy_account_id, status,
                       lot_mode, lot_multiplier, min_lot, max_lot,
                       max_positions, max_daily_loss, max_consecutive_losses,
                       direction_mode, copy_sl, copy_tp, sync_mode,
                       retry_on_failure, retry_max)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                   RETURNING relationship_id, master_account_id, copy_account_id,
                             status, lot_mode, lot_multiplier, min_lot, max_lot,
                             max_positions, max_daily_loss, max_consecutive_losses,
                             direction_mode, copy_sl, copy_tp, sync_mode,
                             created_at, updated_at""",
                body.master_account_id,      # $1
                body.copy_account_id,        # $2  → copy_account_id in DB
                body.status,                 # $3
                body.lot_mode,               # $4
                body.lot_multiplier,         # $5
                body.min_lot,                # $6
                body.max_lot,                # $7
                body.max_positions,          # $8
                body.max_daily_loss,         # $9
                body.max_consecutive_losses, # $10
                body.direction_mode,         # $11
                body.copy_sl,                # $12
                body.copy_tp,                # $13
                body.sync_mode,              # $14
                body.retry_on_failure,       # $15
                body.retry_max,              # $16
            )

            item = _row_to_relationship(dict(row))

            # ── Broadcast config invalidation ───
            if config_provider is not None:
                try:
                    await config_provider.set(
                        f"copy.relationship.{row['relationship_id']}.status",
                        body.status,
                    )
                except Exception:
                    pass  # best-effort; the DB write is what matters

            return {"code": 0, "data": item, "message": "ok"}

        except Exception as exc:
            logger.error("create_relationship failed: %s", exc)
            return {"code": "WB_CFG_002", "data": None, "message": str(exc)}

    # ═══════════════════════════════════════════
    #  Relationships — Detail
    # ═══════════════════════════════════════════

    @router.get("/relationships/{relationship_id}")
    async def get_relationship(
        relationship_id: int,
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Get a single copy relationship by ID."""
        if not _db_ok():
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            row = await db_pool.fetchrow(
                """SELECT relationship_id, master_account_id, copy_account_id,
                          status, lot_mode, lot_multiplier, min_lot, max_lot,
                          max_positions, max_daily_loss, max_consecutive_losses,
                          direction_mode, copy_sl, copy_tp, sync_mode,
                          created_at, updated_at
                   FROM hcm_copy.relationships
                   WHERE relationship_id = $1""",
                relationship_id,
            )

            if row is None:
                return {
                    "code": "CFG_LOAD_002",
                    "data": None,
                    "message": f"Copy relationship not found: {relationship_id}",
                }

            return {"code": 0, "data": _row_to_relationship(dict(row)), "message": "ok"}

        except Exception as exc:
            logger.error("get_relationship failed for relationship_id=%s: %s", relationship_id, exc)
            return {"code": "WB_CFG_001", "data": None, "message": str(exc)}

    # ═══════════════════════════════════════════
    #  Relationships — Update
    # ═══════════════════════════════════════════

    @router.put("/relationships/{relationship_id}")
    async def update_relationship(
        relationship_id: int,
        body: CopyRelationshipUpdate,
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Update a copy relationship.

        Only fields explicitly provided in the request body are updated.
        Status changes (running/stopped/paused) are broadcast via config_provider.
        """
        if not _db_ok():
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            # ── Verify existence ────────────────
            existing = await db_pool.fetchrow(
                "SELECT relationship_id FROM hcm_copy.relationships WHERE relationship_id = $1",
                relationship_id,
            )
            if existing is None:
                return {
                    "code": "CFG_LOAD_002",
                    "data": None,
                    "message": f"Copy relationship not found: {relationship_id}",
                }

            # ── Build dynamic SET clause ────────
            update_data = body.model_dump(exclude_unset=True)
            # Remove display-only fields that are not in the DB schema
            update_data.pop("max_slippage_pips", None)
            update_data.pop("max_execution_delay_ms", None)
            if not update_data:
                return {"code": 0, "data": None, "message": "No fields to update"}

            set_parts: list[str] = []
            params: list[Any] = []
            idx = 1

            for field, value in update_data.items():
                set_parts.append(f"{field} = ${idx}")
                params.append(value)
                idx += 1

            # Always bump updated_at
            set_parts.append(f"updated_at = NOW()")

            params.append(relationship_id)
            set_clause = ", ".join(set_parts)

            row = await db_pool.fetchrow(
                f"""UPDATE hcm_copy.relationships
                    SET {set_clause}
                    WHERE relationship_id = ${idx}
                    RETURNING relationship_id, master_account_id, copy_account_id,
                              status, lot_mode, lot_multiplier, min_lot, max_lot,
                              max_positions, max_daily_loss, max_consecutive_losses,
                              direction_mode, copy_sl, copy_tp, sync_mode,
                              created_at, updated_at""",
                *params,
            )

            # ── Broadcast status change ─────────
            if config_provider is not None and "status" in update_data:
                try:
                    await config_provider.set(
                        f"copy.relationship.{relationship_id}.status",
                        update_data["status"],
                    )
                except Exception:
                    pass

            return {"code": 0, "data": _row_to_relationship(dict(row)), "message": "ok"}

        except Exception as exc:
            logger.error("update_relationship failed for relationship_id=%s: %s", relationship_id, exc)
            return {"code": "WB_CFG_002", "data": None, "message": str(exc)}

    # ═══════════════════════════════════════════
    #  Relationships — Delete
    # ═══════════════════════════════════════════

    @router.delete("/relationships/{relationship_id}")
    async def delete_relationship(
        relationship_id: int,
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Delete a copy relationship by ID."""
        if not _db_ok():
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            result = await db_pool.execute(
                "DELETE FROM hcm_copy.relationships WHERE relationship_id = $1",
                relationship_id,
            )

            # asyncpg execute returns a string like "DELETE 1"; check affected
            # by doing a follow-up query for the common case
            # (execute return value varies by pool wrapper; we rely on the
            #  absence of an exception to signal success)
            return {"code": 0, "data": {"relationship_id": relationship_id, "deleted": True}, "message": "ok"}

        except Exception as exc:
            logger.error("delete_relationship failed for relationship_id=%s: %s", relationship_id, exc)
            return {"code": "WB_CFG_002", "data": None, "message": str(exc)}

    # ═══════════════════════════════════════════
    #  Relationships — Trade Logs (read-only)
    # ═══════════════════════════════════════════

    @router.get("/relationships/{relationship_id}/logs")
    async def list_relationship_logs(
        relationship_id: int,
        request: Request,
        user=Depends(auth_handler.require_auth),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
    ):
        """List trade logs for a specific copy relationship (paginated)."""
        if not _db_ok():
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            page, page_size = _clamp_page(page, page_size)

            # Verify the relationship exists
            rel = await db_pool.fetchrow(
                "SELECT relationship_id FROM hcm_copy.relationships WHERE relationship_id = $1",
                relationship_id,
            )
            if rel is None:
                return {
                    "code": "CFG_LOAD_002",
                    "data": None,
                    "message": f"Copy relationship not found: {relationship_id}",
                }

            # Count
            count_row = await db_pool.fetchrow(
                "SELECT COUNT(*) FROM hcm_copy.trade_logs WHERE relationship_id = $1",
                relationship_id,
            )
            total = count_row[0] if count_row else 0

            # Fetch
            offset = (page - 1) * page_size
            rows = await db_pool.fetch(
                """SELECT log_id, relationship_id, signal_id, symbol, direction,
                          lot, entry_price, exit_price, pnl,
                          status, message, created_at
                   FROM hcm_copy.trade_logs
                   WHERE relationship_id = $1
                   ORDER BY created_at DESC
                   LIMIT $2 OFFSET $3""",
                relationship_id, page_size, offset,
            )

            items = [_row_to_trade_log(dict(r)) for r in rows]

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
            logger.error("list_relationship_logs failed for relationship_id=%s: %s", relationship_id, exc)
            return {"code": "WB_CFG_001", "data": None, "message": str(exc)}

    # ═══════════════════════════════════════════
    #  Symbol Mappings — List
    # ═══════════════════════════════════════════

    @router.get("/symbol-mappings")
    async def list_symbol_mappings(
        request: Request,
        user=Depends(auth_handler.require_auth),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
    ):
        """List all cross-broker symbol mappings (paginated)."""
        if not _db_ok():
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            page, page_size = _clamp_page(page, page_size)

            count_row = await db_pool.fetchrow(
                "SELECT COUNT(*) FROM hcm_copy.symbol_mappings"
            )
            total = count_row[0] if count_row else 0

            offset = (page - 1) * page_size
            rows = await db_pool.fetch(
                """SELECT mapping_id, master_broker, master_symbol,
                          follower_broker, follower_symbol,
                          match_mode, match_priority, is_active,
                          created_at, updated_at
                   FROM hcm_copy.symbol_mappings
                   ORDER BY match_priority DESC, mapping_id ASC
                   LIMIT $1 OFFSET $2""",
                page_size, offset,
            )

            items = [_row_to_mapping(dict(r)) for r in rows]

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
            logger.error("list_symbol_mappings failed: %s", exc)
            return {"code": "WB_CFG_001", "data": None, "message": str(exc)}

    # ═══════════════════════════════════════════
    #  Symbol Mappings — Create
    # ═══════════════════════════════════════════

    @router.post("/symbol-mappings")
    async def create_symbol_mapping(
        request: Request,
        body: SymbolMappingCreate,
        user=Depends(auth_handler.require_auth),
    ):
        """Create a new cross-broker symbol mapping.

        Mappings are created with is_active=False by default (safety constraint).
        """
        if not _db_ok():
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            row = await db_pool.fetchrow(
                """INSERT INTO hcm_copy.symbol_mappings
                      (master_broker, master_symbol,
                       follower_broker, follower_symbol,
                       match_mode, match_priority, is_active)
                   VALUES ($1,$2,$3,$4,$5,$6,$7)
                   RETURNING mapping_id, master_broker, master_symbol,
                             follower_broker, follower_symbol,
                             match_mode, match_priority, is_active,
                             created_at, updated_at""",
                body.master_broker,
                body.master_symbol,
                body.follower_broker,
                body.follower_symbol,
                body.match_mode,
                body.match_priority,
                body.is_active,
            )

            return {"code": 0, "data": _row_to_mapping(dict(row)), "message": "ok"}

        except Exception as exc:
            logger.error("create_symbol_mapping failed: %s", exc)
            return {"code": "WB_CFG_002", "data": None, "message": str(exc)}

    # ═══════════════════════════════════════════
    #  Symbol Mappings — Update
    # ═══════════════════════════════════════════

    @router.put("/symbol-mappings/{mapping_id}")
    async def update_symbol_mapping(
        mapping_id: int,
        body: SymbolMappingUpdate,
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Update a symbol mapping.

        Only fields explicitly provided in the request body are updated.
        """
        if not _db_ok():
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            # Verify existence
            existing = await db_pool.fetchrow(
                "SELECT mapping_id FROM hcm_copy.symbol_mappings WHERE mapping_id = $1",
                mapping_id,
            )
            if existing is None:
                return {
                    "code": "CFG_LOAD_002",
                    "data": None,
                    "message": f"Symbol mapping not found: {mapping_id}",
                }

            update_data = body.model_dump(exclude_unset=True)
            if not update_data:
                return {"code": 0, "data": None, "message": "No fields to update"}

            set_parts: list[str] = []
            params: list[Any] = []
            idx = 1

            for field, value in update_data.items():
                set_parts.append(f"{field} = ${idx}")
                params.append(value)
                idx += 1

            set_parts.append(f"updated_at = NOW()")
            params.append(mapping_id)
            set_clause = ", ".join(set_parts)

            row = await db_pool.fetchrow(
                f"""UPDATE hcm_copy.symbol_mappings
                    SET {set_clause}
                    WHERE mapping_id = ${idx}
                    RETURNING mapping_id, master_broker, master_symbol,
                              follower_broker, follower_symbol,
                              match_mode, match_priority, is_active,
                              created_at, updated_at""",
                *params,
            )

            return {"code": 0, "data": _row_to_mapping(dict(row)), "message": "ok"}

        except Exception as exc:
            logger.error("update_symbol_mapping failed for mapping_id=%s: %s", mapping_id, exc)
            return {"code": "WB_CFG_002", "data": None, "message": str(exc)}

    # ═══════════════════════════════════════════
    #  Symbol Mappings — Delete
    # ═══════════════════════════════════════════

    @router.delete("/symbol-mappings/{mapping_id}")
    async def delete_symbol_mapping(
        mapping_id: int,
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Delete a symbol mapping by ID."""
        if not _db_ok():
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            await db_pool.execute(
                "DELETE FROM hcm_copy.symbol_mappings WHERE mapping_id = $1",
                mapping_id,
            )

            return {"code": 0, "data": {"mapping_id": mapping_id, "deleted": True}, "message": "ok"}

        except Exception as exc:
            logger.error("delete_symbol_mapping failed for mapping_id=%s: %s", mapping_id, exc)
            return {"code": "WB_CFG_002", "data": None, "message": str(exc)}

    # ═══════════════════════════════════════════
    #  Symbol Mappings — Toggle
    # ═══════════════════════════════════════════

    @router.put("/symbol-mappings/{mapping_id}/toggle")
    async def toggle_symbol_mapping(
        mapping_id: int,
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Toggle a symbol mapping between active and inactive.

        Reads the current is_active state and flips it.
        """
        if not _db_ok():
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            existing = await db_pool.fetchrow(
                "SELECT mapping_id, is_active FROM hcm_copy.symbol_mappings WHERE mapping_id = $1",
                mapping_id,
            )
            if existing is None:
                return {
                    "code": "CFG_LOAD_002",
                    "data": None,
                    "message": f"Symbol mapping not found: {mapping_id}",
                }

            new_active = not existing["is_active"]

            row = await db_pool.fetchrow(
                """UPDATE hcm_copy.symbol_mappings
                   SET is_active = $1, updated_at = NOW()
                   WHERE mapping_id = $2
                   RETURNING mapping_id, master_broker, master_symbol,
                             follower_broker, follower_symbol,
                             match_mode, match_priority, is_active,
                             created_at, updated_at""",
                new_active,
                mapping_id,
            )

            return {"code": 0, "data": _row_to_mapping(dict(row)), "message": "ok"}

        except Exception as exc:
            logger.error("toggle_symbol_mapping failed for mapping_id=%s: %s", mapping_id, exc)
            return {"code": "WB_CFG_002", "data": None, "message": str(exc)}

    # ═══════════════════════════════════════════
    #  Accounts — List (for dropdowns)
    # ═══════════════════════════════════════════

    @router.get("/accounts")
    async def list_accounts(
        request: Request,
        user=Depends(auth_handler.require_auth),
        account_type: Optional[str] = Query(None, description="Filter by account type (master/follower)"),
    ):
        """Return all active accounts, optionally filtered by type.

        Used by the frontend to populate master/follower account dropdowns.
        """
        if not _db_ok():
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            conditions: list[str] = ["is_active = true"]
            params: list[Any] = []
            idx = 1

            if account_type is not None:
                conditions.append(f"account_type = ${idx}")
                params.append(account_type)
                idx += 1

            where = " AND ".join(conditions)

            rows = await db_pool.fetch(
                f"""SELECT account_id, account_name, account_number,
                           broker_name, server_name, account_type
                    FROM hcm_broker.accounts
                    WHERE {where}
                    ORDER BY account_type, account_id""",
                *params,
            )

            items = [dict(r) for r in rows]
            return {"code": 0, "data": items, "message": "ok"}

        except Exception as exc:
            logger.error("list_accounts failed: %s", exc)
            return {"code": "WB_CFG_001", "data": None, "message": str(exc)}

    # ═══════════════════════════════════════════
    #  Brokers — List distinct
    # ═══════════════════════════════════════════

    @router.get("/brokers")
    async def list_brokers(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Return distinct broker names."""
        if not _db_ok():
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            rows = await db_pool.fetch(
                """SELECT DISTINCT broker_name
                   FROM hcm_broker.accounts
                   WHERE broker_name IS NOT NULL
                   ORDER BY broker_name"""
            )

            items = [{"broker_name": r["broker_name"]} for r in rows]
            return {"code": 0, "data": items, "message": "ok"}

        except Exception as exc:
            logger.error("list_brokers failed: %s", exc)
            return {"code": "WB_CFG_001", "data": None, "message": str(exc)}

    # ═══════════════════════════════════════════
    #  Legacy Backwards-Compatibility Aliases
    # ═══════════════════════════════════════════

    legacy_router = APIRouter(prefix="/api/copy", tags=["copy-legacy"])

    @legacy_router.get("/config")
    async def legacy_get_config(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Legacy GET /api/copy/config — returns relationships + symbol mappings.

        Adapts the old frontend path to the new v1 data model.
        Returns both relationships and symbol mappings in a single response.
        """
        if not _db_ok():
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            # Fetch all relationships
            rel_rows = await db_pool.fetch(
                """SELECT relationship_id, master_account_id, copy_account_id,
                          status, lot_mode, lot_multiplier, min_lot, max_lot,
                          max_positions, max_daily_loss, max_consecutive_losses,
                          direction_mode, copy_sl, copy_tp, sync_mode,
                          created_at, updated_at
                   FROM hcm_copy.relationships
                   ORDER BY relationship_id DESC"""
            )
            relationships = [_row_to_relationship(dict(r)) for r in rel_rows]

            # Fetch all symbol mappings
            map_rows = await db_pool.fetch(
                """SELECT mapping_id, master_broker, master_symbol,
                          follower_broker, follower_symbol,
                          match_mode, match_priority, is_active,
                          created_at, updated_at
                   FROM hcm_copy.symbol_mappings
                   ORDER BY match_priority DESC, mapping_id ASC"""
            )
            mappings = [_row_to_mapping(dict(r)) for r in map_rows]

            return {
                "code": 0,
                "data": {
                    "relationships": relationships,
                    "symbol_mappings": mappings,
                },
                "message": "ok",
            }

        except Exception as exc:
            logger.error("legacy_get_config failed: %s", exc)
            return {"code": "WB_CFG_001", "data": None, "message": str(exc)}

    @legacy_router.put("/config")
    async def legacy_put_config(
        request: Request,
        body: LegacyCopyConfigRequest,
        user=Depends(auth_handler.require_auth),
    ):
        """Legacy PUT /api/copy/config — parse old frontend fields and write.

        Maps legacy flat-object fields to the new relationships model.
        If the master/follower pair already exists, updates it; otherwise creates.
        """
        if not _db_ok():
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            master_id = body.master_account_id
            copy_id = body.copy_account_id

            if master_id is None or copy_id is None:
                return {
                    "code": "COPY_002",
                    "data": None,
                    "message": "master_account_id and copy_account_id are required",
                }

            # Check for existing relationship
            existing = await db_pool.fetchrow(
                """SELECT relationship_id FROM hcm_copy.relationships
                   WHERE master_account_id = $1 AND copy_account_id = $2""",
                master_id, copy_id,
            )

            # Collect non-None fields from the legacy body (exclude the ID fields)
            update_data = body.model_dump(exclude_unset=True, exclude={"master_account_id", "copy_account_id"})

            if existing is not None:
                # ── Update existing ─────────────
                relationship_id = existing["relationship_id"]
                if not update_data:
                    row = await db_pool.fetchrow(
                        """SELECT relationship_id, master_account_id, copy_account_id,
                                  status, lot_mode, lot_multiplier, min_lot, max_lot,
                                  max_positions, max_daily_loss, max_consecutive_losses,
                                  direction_mode, copy_sl, copy_tp, sync_mode,
                                  created_at, updated_at
                           FROM hcm_copy.relationships WHERE relationship_id = $1""",
                        relationship_id,
                    )
                    return {"code": 0, "data": _row_to_relationship(dict(row)), "message": "ok"}

                set_parts: list[str] = []
                params: list[Any] = []
                idx = 1
                for field, value in update_data.items():
                    set_parts.append(f"{field} = ${idx}")
                    params.append(value)
                    idx += 1
                set_parts.append("updated_at = NOW()")
                params.append(relationship_id)
                set_clause = ", ".join(set_parts)

                row = await db_pool.fetchrow(
                    f"""UPDATE hcm_copy.relationships
                        SET {set_clause}
                        WHERE relationship_id = ${idx}
                        RETURNING relationship_id, master_account_id, copy_account_id,
                                  status, lot_mode, lot_multiplier, min_lot, max_lot,
                                  max_positions, max_daily_loss, max_consecutive_losses,
                                  direction_mode, copy_sl, copy_tp, sync_mode,
                                  created_at, updated_at""",
                    *params,
                )
            else:
                # ── Create new ──────────────────
                row = await db_pool.fetchrow(
                    """INSERT INTO hcm_copy.relationships
                          (master_account_id, copy_account_id, status,
                           lot_mode, lot_multiplier, min_lot, max_lot,
                           max_positions, max_daily_loss, max_consecutive_losses,
                           direction_mode, copy_sl, copy_tp, sync_mode)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
                       RETURNING relationship_id, master_account_id, copy_account_id,
                                 status, lot_mode, lot_multiplier, min_lot, max_lot,
                                 max_positions, max_daily_loss, max_consecutive_losses,
                                 direction_mode, copy_sl, copy_tp, sync_mode,
                                 created_at, updated_at""",
                    master_id,
                    copy_id,
                    update_data.get("status", "stopped"),
                    update_data.get("lot_mode", "multiplier"),
                    update_data.get("lot_multiplier", 1.0),
                    update_data.get("min_lot", 0.01),
                    update_data.get("max_lot", 5.0),
                    update_data.get("max_positions", 10),
                    update_data.get("max_daily_loss", 0.0),
                    update_data.get("max_consecutive_losses", 3),
                    update_data.get("direction_mode", "FORWARD"),
                    update_data.get("copy_sl", True),
                    update_data.get("copy_tp", True),
                    update_data.get("sync_mode", "pubsub"),
                )

            return {"code": 0, "data": _row_to_relationship(dict(row)), "message": "ok"}

        except Exception as exc:
            logger.error("legacy_put_config failed: %s", exc)
            return {"code": "WB_CFG_002", "data": None, "message": str(exc)}

    return router, legacy_router
