"""Symbols API — Symbol management and activation.

Provides:
- GET /api/v1/symbols — List symbols with optional filtering
- POST /api/v1/symbols — Register new symbol
- PUT /api/v1/symbols/{symbol}/activate — Activate a symbol
- PUT /api/v1/symbols/{symbol}/deactivate — Deactivate a symbol
- GET /api/v1/symbols/{symbol}/category — Get symbol→category mapping
- GET /api/v1/symbols/categories — List all categories
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ── Models ─────────────────────────────────────

class SymbolRegisterRequest(BaseModel):
    """Symbol registration request."""
    symbol: str = Field(..., min_length=2, max_length=20, description="Trading symbol code")
    category: str = Field(..., min_length=2, max_length=20, description="Category: metals/crypto/forex")
    display_name: str = Field("", max_length=50)
    base_currency: str = Field("USD", max_length=10)
    quote_currency: str = Field("", max_length=10)
    lot_step: float = Field(0.01, gt=0)
    min_lot: float = Field(0.01, gt=0)
    max_lot: float = Field(5.0, gt=0)
    pip_value: Optional[float] = Field(None)
    phase: int = Field(1, ge=1)


class SymbolUpdateRequest(BaseModel):
    """Symbol update request."""
    display_name: Optional[str] = Field(None, max_length=50)
    category: Optional[str] = Field(None, max_length=20)
    lot_step: Optional[float] = Field(None, gt=0)
    min_lot: Optional[float] = Field(None, gt=0)
    max_lot: Optional[float] = Field(None, gt=0)
    pip_value: Optional[float] = None
    phase: Optional[int] = Field(None, ge=1)


class SymbolResponse(BaseModel):
    """Symbol metadata response."""
    symbol: str = ""
    category: str = ""
    display_name: str = ""
    base_currency: str = "USD"
    quote_currency: str = ""
    lot_step: float = 0.01
    min_lot: float = 0.01
    max_lot: float = 5.0
    pip_value: Optional[float] = None
    is_active: bool = True
    phase: int = 1
    created_at: str = ""
    updated_at: str = ""


# ── Router Factory ─────────────────────────────

def create_symbols_router(
    db_pool: Any = None,
    config_provider: Any = None,
    auth_handler: Any = None,
) -> APIRouter:
    """Create FastAPI router with symbol management endpoints.

    Args:
        db_pool: DatabasePool instance.
        config_provider: ConfigProviderV3 instance.
        auth_handler: AuthHandler instance for RBAC.

    Returns:
        APIRouter with symbol routes.
    """
    router = APIRouter(prefix="/api/v1/symbols", tags=["symbols"])

    # ── List Symbols ────────────────────────────

    @router.get("")
    async def list_symbols(
        request: Request,
        category: Optional[str] = Query(None, description="Filter by category"),
        is_active: Optional[bool] = Query(None, description="Filter by activation status"),
        search: Optional[str] = Query(None, description="Search in symbol/display_name"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=100),
    ):
        """List registered symbols with filtering.

        Args:
            category: Filter by category (metals/crypto/forex).
            is_active: Filter by active status.
            search: Free-text search.
            page: Page number.
            page_size: Items per page.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            conditions = ["1=1"]
            params: list[Any] = []
            param_idx = 1

            if category:
                conditions.append(f"category = ${param_idx}")
                params.append(category)
                param_idx += 1

            if is_active is not None:
                conditions.append(f"is_active = ${param_idx}")
                params.append(is_active)
                param_idx += 1

            if search:
                conditions.append(
                    f"(symbol ILIKE ${param_idx} OR display_name ILIKE ${param_idx})"
                )
                params.append(f"%{search}%")
                param_idx += 1

            where_clause = " AND ".join(conditions)

            # Count
            count_row = await db_pool.fetchrow(
                f"SELECT COUNT(*) FROM hcm_config.symbol_meta WHERE {where_clause}",
                *params,
            )
            total = count_row[0] if count_row else 0

            # Fetch
            offset = (page - 1) * page_size
            rows = await db_pool.fetch(
                f"""SELECT symbol, category, display_name, base_currency, quote_currency,
                           lot_step, min_lot, max_lot, pip_value, is_active, phase,
                           created_at, updated_at
                    FROM hcm_config.symbol_meta
                    WHERE {where_clause}
                    ORDER BY category, symbol
                    LIMIT ${param_idx} OFFSET ${param_idx + 1}""",
                *params, page_size, offset,
            )

            items = []
            for row in rows:
                item = dict(row)
                if item.get("created_at"):
                    item["created_at"] = item["created_at"].isoformat()
                if item.get("updated_at"):
                    item["updated_at"] = item["updated_at"].isoformat()
                # Convert Decimal to float for JSON
                for key in ("lot_step", "min_lot", "max_lot", "pip_value"):
                    if item.get(key) is not None:
                        item[key] = float(item[key])
                items.append(item)

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
            logger.error("Symbol list failed: %s", exc)
            return {"code": "SYS_DB_001", "data": None, "message": str(exc)}

    # ── Register Symbol ─────────────────────────

    @router.post("")
    async def register_symbol(body: SymbolRegisterRequest):
        """Register a new trading symbol.

        Args:
            body: Symbol registration data.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            await db_pool.execute(
                """INSERT INTO hcm_config.symbol_meta
                   (symbol, category, display_name, base_currency, quote_currency,
                    lot_step, min_lot, max_lot, pip_value, is_active, phase)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, true, $10)
                   ON CONFLICT (symbol) DO NOTHING""",
                body.symbol.upper(),
                body.category.lower(),
                body.display_name or body.symbol.upper(),
                body.base_currency,
                body.quote_currency or body.base_currency,
                body.lot_step,
                body.min_lot,
                body.max_lot,
                body.pip_value,
                body.phase,
            )

            logger.info("Symbol registered: %s (category=%s)", body.symbol, body.category)
            return {
                "code": 0,
                "data": {"symbol": body.symbol.upper(), "category": body.category},
                "message": "Symbol registered successfully",
            }

        except Exception as exc:
            logger.error("Symbol register failed for %s: %s", body.symbol, exc)
            return {"code": "SYS_DB_001", "data": None, "message": str(exc)}

    # ── Activate / Deactivate ───────────────────

    @router.put("/{symbol}/activate")
    async def activate_symbol(symbol: str):
        """Activate a trading symbol.

        Args:
            symbol: Trading symbol code.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            result = await db_pool.execute(
                """UPDATE hcm_config.symbol_meta
                   SET is_active = true, updated_at = now()
                   WHERE symbol = $1""",
                symbol.upper(),
            )
            if "UPDATE 0" in result:
                return {"code": "CFG_LOAD_002", "data": None, "message": f"Symbol not found: {symbol}"}

            logger.info("Symbol activated: %s", symbol)
            return {"code": 0, "data": {"symbol": symbol.upper(), "is_active": True}, "message": "ok"}

        except Exception as exc:
            logger.error("Symbol activate failed for %s: %s", symbol, exc)
            return {"code": "SYS_DB_001", "data": None, "message": str(exc)}

    @router.put("/{symbol}/deactivate")
    async def deactivate_symbol(symbol: str):
        """Deactivate a trading symbol.

        Args:
            symbol: Trading symbol code.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            result = await db_pool.execute(
                """UPDATE hcm_config.symbol_meta
                   SET is_active = false, updated_at = now()
                   WHERE symbol = $1""",
                symbol.upper(),
            )
            if "UPDATE 0" in result:
                return {"code": "CFG_LOAD_002", "data": None, "message": f"Symbol not found: {symbol}"}

            logger.info("Symbol deactivated: %s", symbol)
            return {"code": 0, "data": {"symbol": symbol.upper(), "is_active": False}, "message": "ok"}

        except Exception as exc:
            logger.error("Symbol deactivate failed for %s: %s", symbol, exc)
            return {"code": "SYS_DB_001", "data": None, "message": str(exc)}

    # ── Category Mapping ────────────────────────

    @router.get("/{symbol}/category")
    async def get_symbol_category(symbol: str):
        """Get category for a specific symbol.

        Args:
            symbol: Trading symbol code.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            row = await db_pool.fetchrow(
                """SELECT symbol, category, display_name, is_active
                   FROM hcm_config.symbol_meta
                   WHERE symbol = $1""",
                symbol.upper(),
            )

            if row is None:
                return {"code": "CFG_LOAD_002", "data": None, "message": f"Symbol not found: {symbol}"}

            return {
                "code": 0,
                "data": {
                    "symbol": row["symbol"],
                    "category": row["category"],
                    "display_name": row["display_name"],
                    "is_active": row["is_active"],
                },
                "message": "ok",
            }

        except Exception as exc:
            logger.error("Symbol category lookup failed for %s: %s", symbol, exc)
            return {"code": "SYS_DB_001", "data": None, "message": str(exc)}

    # ── Categories ──────────────────────────────

    @router.get("/categories/list")
    async def list_categories():
        """List all symbol categories with counts."""
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            rows = await db_pool.fetch(
                """SELECT category, COUNT(*) as total, COUNT(*) FILTER (WHERE is_active) as active
                   FROM hcm_config.symbol_meta
                   GROUP BY category
                   ORDER BY category"""
            )
            categories = [
                {"category": r["category"], "total": r["total"], "active": r["active"]}
                for r in rows
            ]
            return {"code": 0, "data": categories, "message": "ok"}

        except Exception as exc:
            logger.error("Symbol categories list failed: %s", exc)
            return {"code": "SYS_DB_001", "data": None, "message": str(exc)}

    return router
