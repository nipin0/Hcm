"""Positions API — Current and historical position queries.

Provides:
- GET /api/v1/positions — List current positions
- GET /api/v1/positions/history — Historical position query
- GET /api/v1/positions/{position_id} — Position detail with P&L
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

logger = logging.getLogger(__name__)


# ── Router Factory ─────────────────────────────

def create_positions_router(
    db_pool: Any = None,
    redis_client: Any = None,
    auth_handler: Any = None,
) -> APIRouter:
    """Create FastAPI router with position query endpoints.

    Args:
        db_pool: DatabasePool instance.
        redis_client: RedisClient instance.
        auth_handler: AuthHandler instance for RBAC.

    Returns:
        APIRouter with position routes.
    """
    router = APIRouter(prefix="/api/v1/positions", tags=["positions"])

    # ── List Current Positions ──────────────────

    @router.get("")
    async def list_positions(
        request: Request,
        account_id: Optional[int] = Query(None, description="Filter by account"),
        symbol: Optional[str] = Query(None, description="Filter by symbol"),
        direction: Optional[str] = Query(None, description="Filter by direction"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=100),
    ):
        """List current open positions.

        Returns most recent snapshot for each active position.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            conditions = ["1=1"]
            params: list[Any] = []
            param_idx = 1

            if account_id is not None:
                conditions.append(f"p.account_id = ${param_idx}")
                params.append(account_id)
                param_idx += 1

            if symbol:
                conditions.append(f"p.symbol = ${param_idx}")
                params.append(symbol.upper())
                param_idx += 1

            if direction:
                conditions.append(f"p.direction = ${param_idx}")
                params.append(direction.upper())
                param_idx += 1

            where_clause = " AND ".join(conditions)

            # Count
            count_row = await db_pool.fetchrow(
                f"""SELECT COUNT(DISTINCT p.mt5_ticket)
                    FROM hcm_trading.positions p
                    WHERE {where_clause}""",
                *params,
            )
            total = count_row[0] if count_row else 0

            # Fetch latest snapshot per position
            offset = (page - 1) * page_size
            rows = await db_pool.fetch(
                f"""SELECT DISTINCT ON (p.mt5_ticket)
                           p.position_id, p.account_id, p.order_id, p.mt5_ticket,
                           p.symbol, p.direction, p.open_price, p.current_price,
                           p.lot, p.sl, p.tp, p.float_profit, p.trail_state,
                           p.open_time, p.snapshot_time
                    FROM hcm_trading.positions p
                    WHERE {where_clause}
                    ORDER BY p.mt5_ticket, p.snapshot_time DESC
                    LIMIT ${param_idx} OFFSET ${param_idx + 1}""",
                *params, page_size, offset,
            )

            items = []
            for row in rows:
                item = dict(row)
                for key in ("open_price", "current_price", "lot", "sl", "tp", "float_profit"):
                    if item.get(key) is not None:
                        item[key] = float(item[key])
                if item.get("open_time"):
                    item["open_time"] = item["open_time"].isoformat()
                if item.get("snapshot_time"):
                    item["snapshot_time"] = item["snapshot_time"].isoformat()
                # Calculate unrealized P&L
                if item.get("open_price") and item.get("current_price") and item.get("lot"):
                    price_diff = item["current_price"] - item["open_price"]
                    if item.get("direction") == "SELL":
                        price_diff = -price_diff
                    item["unrealized_pnl"] = round(price_diff * float(item["lot"]) * 100, 2)
                else:
                    item["unrealized_pnl"] = item.get("float_profit", 0.0)
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
            logger.error("Position list failed: %s", exc)
            return {"code": "SYS_DB_001", "data": None, "message": str(exc)}

    # ── Position History ────────────────────────

    @router.get("/history")
    async def position_history(
        request: Request,
        account_id: Optional[int] = Query(None),
        symbol: Optional[str] = Query(None),
        from_date: Optional[str] = Query(None, description="From date ISO"),
        to_date: Optional[str] = Query(None, description="To date ISO"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
    ):
        """Query historical (closed) positions from orders table.

        Args:
            account_id: Filter by account.
            symbol: Filter by symbol.
            from_date: Start date.
            to_date: End date.
            page: Page number.
            page_size: Items per page.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            conditions = ["o.close_time IS NOT NULL"]  # Only closed orders
            params: list[Any] = []
            param_idx = 1

            if account_id is not None:
                conditions.append(f"o.account_id = ${param_idx}")
                params.append(account_id)
                param_idx += 1

            if symbol:
                conditions.append(f"o.symbol = ${param_idx}")
                params.append(symbol.upper())
                param_idx += 1

            if from_date:
                conditions.append(f"o.close_time >= ${param_idx}")
                params.append(from_date)
                param_idx += 1

            if to_date:
                conditions.append(f"o.close_time <= ${param_idx}")
                params.append(to_date)
                param_idx += 1

            where_clause = " AND ".join(conditions)

            count_row = await db_pool.fetchrow(
                f"SELECT COUNT(*) FROM hcm_trading.orders o WHERE {where_clause}",
                *params,
            )
            total = count_row[0] if count_row else 0

            offset = (page - 1) * page_size
            rows = await db_pool.fetch(
                f"""SELECT o.order_id, o.signal_id, o.account_id, o.mt5_ticket,
                           o.symbol, o.direction, o.open_price, o.close_price,
                           o.lot, o.sl, o.tp, o.commission, o.swap, o.profit,
                           o.order_status, o.open_time, o.close_time, o.close_reason
                    FROM hcm_trading.orders o
                    WHERE {where_clause}
                    ORDER BY o.close_time DESC
                    LIMIT ${param_idx} OFFSET ${param_idx + 1}""",
                *params, page_size, offset,
            )

            items = []
            for row in rows:
                item = dict(row)
                for key in ("open_price", "close_price", "lot", "sl", "tp",
                           "commission", "swap", "profit"):
                    if item.get(key) is not None:
                        item[key] = float(item[key])
                if item.get("open_time"):
                    item["open_time"] = item["open_time"].isoformat()
                if item.get("close_time"):
                    item["close_time"] = item["close_time"].isoformat()
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
            logger.error("Position history failed: %s", exc)
            return {"code": "SYS_DB_001", "data": None, "message": str(exc)}

    # ── Position Detail ─────────────────────────

    @router.get("/{position_id}")
    async def get_position(position_id: int):
        """Get detailed position information with P&L.

        Args:
            position_id: Position ID.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            row = await db_pool.fetchrow(
                """SELECT p.*, a.account_name, a.broker_name
                   FROM hcm_trading.positions p
                   LEFT JOIN hcm_broker.accounts a ON p.account_id = a.account_id
                   WHERE p.position_id = $1
                   ORDER BY p.snapshot_time DESC LIMIT 1""",
                position_id,
            )

            if row is None:
                return {"code": "CFG_LOAD_002", "data": None, "message": f"Position not found: {position_id}"}

            item = dict(row)
            for key in ("open_price", "current_price", "lot", "sl", "tp", "float_profit"):
                if item.get(key) is not None:
                    item[key] = float(item[key])
            if item.get("open_time"):
                item["open_time"] = item["open_time"].isoformat()
            if item.get("snapshot_time"):
                item["snapshot_time"] = item["snapshot_time"].isoformat()

            # Calculate unrealized P&L
            if item.get("open_price") and item.get("current_price") and item.get("lot"):
                price_diff = item["current_price"] - item["open_price"]
                if item.get("direction") == "SELL":
                    price_diff = -price_diff
                item["unrealized_pnl"] = round(price_diff * float(item["lot"]) * 100, 2)

            return {"code": 0, "data": item, "message": "ok"}

        except Exception as exc:
            logger.error("Position detail failed for id=%d: %s", position_id, exc)
            return {"code": "SYS_DB_001", "data": None, "message": str(exc)}

    # ── Position Summary ────────────────────────

    @router.get("/summary/overview")
    async def position_summary(account_id: Optional[int] = Query(None)):
        """Get position summary with total P&L.

        Args:
            account_id: Optional account filter.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            account_filter = "AND p.account_id = $1" if account_id is not None else ""
            params: list[Any] = [account_id] if account_id is not None else []

            # Current positions summary
            summary = await db_pool.fetchrow(
                f"""SELECT COUNT(DISTINCT p.mt5_ticket) as open_positions,
                           SUM(p.float_profit) as total_float_profit
                    FROM hcm_trading.positions p
                    WHERE p.snapshot_time >= now() - interval '10 minutes'
                      {account_filter}""",
                *params,
            )

            return {
                "code": 0,
                "data": {
                    "open_positions": summary["open_positions"] if summary else 0,
                    "total_float_profit": round(float(summary["total_float_profit"] or 0), 2) if summary else 0.0,
                    "account_id": account_id,
                },
                "message": "ok",
            }

        except Exception as exc:
            logger.error("Position summary failed: %s", exc)
            return {"code": "SYS_DB_001", "data": None, "message": str(exc)}

    return router
