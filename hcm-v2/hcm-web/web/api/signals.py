"""Signals API — Signal query with filtering and pagination.

Provides:
- GET /api/v1/signals — List signals with pagination and advanced filtering
- GET /api/v1/signals/{signal_id} — Get signal detail
- GET /api/v1/signals/stats — Signal statistics summary

Signal results include v1.3 five-level regime trace fields:
regime, pre_score, weight_scheme.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

logger = logging.getLogger(__name__)


# ── Router Factory ─────────────────────────────

def create_signals_router(
    db_pool: Any = None,
    redis_client: Any = None,
    auth_handler: Any = None,
) -> APIRouter:
    """Create FastAPI router with signal query endpoints.

    Args:
        db_pool: DatabasePool instance.
        redis_client: RedisClient instance.
        auth_handler: AuthHandler instance for RBAC.

    Returns:
        APIRouter with signal routes.
    """
    router = APIRouter(prefix="/api/v1/signals", tags=["signals"])

    # ── List Signals ────────────────────────────

    @router.get("")
    async def list_signals(
        request: Request,
        symbol: Optional[str] = Query(None, description="Filter by symbol"),
        direction: Optional[str] = Query(None, description="Filter by direction (BUY/SELL/NO_TRADE)"),
        regime: Optional[str] = Query(None, description="Filter by regime (PRE_TREND/TREND/etc.)"),
        signal_status: Optional[int] = Query(None, description="Filter by signal status"),
        account_id: Optional[int] = Query(None, description="Filter by account"),
        from_date: Optional[str] = Query(None, description="From date (ISO format)"),
        to_date: Optional[str] = Query(None, description="To date (ISO format)"),
        min_confidence: Optional[float] = Query(None, description="Minimum confidence"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        sort_by: str = Query("created_at", description="Sort field"),
        sort_order: str = Query("desc", description="asc or desc"),
    ):
        """List signals with comprehensive filtering.

        Supports filtering by symbol, direction, regime, status,
        account, date range, and minimum confidence.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            conditions = ["1=1"]
            params: list[Any] = []
            param_idx = 1

            if symbol:
                conditions.append(f"s.symbol = ${param_idx}")
                params.append(symbol.upper())
                param_idx += 1

            if direction:
                conditions.append(f"s.signal_dir = ${param_idx}")
                params.append(direction.upper())
                param_idx += 1

            if regime:
                conditions.append(f"s.regime = ${param_idx}")
                params.append(regime.upper())
                param_idx += 1

            if signal_status is not None:
                conditions.append(f"s.signal_status = ${param_idx}")
                params.append(signal_status)
                param_idx += 1

            if account_id is not None:
                conditions.append(f"s.account_id = ${param_idx}")
                params.append(account_id)
                param_idx += 1

            if from_date:
                conditions.append(f"s.created_at >= ${param_idx}")
                params.append(from_date)
                param_idx += 1

            if to_date:
                conditions.append(f"s.created_at <= ${param_idx}")
                params.append(to_date)
                param_idx += 1

            if min_confidence is not None:
                conditions.append(f"s.confidence >= ${param_idx}")
                params.append(min_confidence)
                param_idx += 1

            where_clause = " AND ".join(conditions)

            # Validate sort
            allowed_sorts = {"created_at", "confidence", "symbol", "signal_id", "pre_score"}
            if sort_by not in allowed_sorts:
                sort_by = "created_at"
            sort_dir = "DESC" if sort_order.lower() == "desc" else "ASC"

            # Count
            count_row = await db_pool.fetchrow(
                f"SELECT COUNT(*) FROM hcm_signal.signals s WHERE {where_clause}",
                *params,
            )
            total = count_row[0] if count_row else 0

            # Fetch
            offset = (page - 1) * page_size
            rows = await db_pool.fetch(
                f"""SELECT s.signal_id, s.task_id, s.account_id, s.symbol, s.time_frame,
                           s.signal_dir, s.entry_price, s.sl_price, s.tp1, s.tp2,
                           s.lot, s.confidence, s.signal_status, s.signal_mode,
                           s.signal_tower_mode, s.fallback_reason,
                           s.regime, s.pre_score, s.weight_scheme, s.position_in_range,
                           s.macro_snapshot_id, s.sentiment_snapshot_id,
                           s.indicator_values, s.composite_score, s.reason,
                           s.created_at, s.updated_at
                    FROM hcm_signal.signals s
                    WHERE {where_clause}
                    ORDER BY s.{sort_by} {sort_dir}
                    LIMIT ${param_idx} OFFSET ${param_idx + 1}""",
                *params, page_size, offset,
            )

            items = []
            for row in rows:
                item = dict(row)
                # Convert decimals to float
                for key in ("entry_price", "sl_price", "tp1", "tp2", "lot", "confidence",
                           "pre_score", "composite_score", "position_in_range"):
                    if item.get(key) is not None:
                        item[key] = float(item[key])
                if item.get("created_at"):
                    item["created_at"] = item["created_at"].isoformat()
                if item.get("updated_at"):
                    item["updated_at"] = item["updated_at"].isoformat()
                if isinstance(item.get("indicator_values"), str):
                    import json
                    try:
                        item["indicator_values"] = json.loads(item["indicator_values"])
                    except (json.JSONDecodeError, TypeError):
                        pass
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
            logger.error("Signal list failed: %s", exc)
            return {"code": "ST_SIG_001", "data": None, "message": str(exc)}

    # ── Get Signal Detail ───────────────────────

    @router.get("/{signal_id}")
    async def get_signal(signal_id: int):
        """Get detailed signal information.

        Returns full signal data including v1.3 regime trace fields
        (regime, pre_score, weight_scheme) and external factor snapshots.

        Args:
            signal_id: Signal ID.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            row = await db_pool.fetchrow(
                """SELECT s.*, t.composite_score as task_composite,
                          t.indicator_values as task_indicators,
                          t.latency_ms as task_latency_ms
                   FROM hcm_signal.signals s
                   LEFT JOIN hcm_signal.tasks t ON s.task_id = t.task_id
                   WHERE s.signal_id = $1""",
                signal_id,
            )

            if row is None:
                return {"code": "ST_SIG_001", "data": None, "message": f"Signal not found: {signal_id}"}

            item = dict(row)
            # Convert decimals to float
            for key in ("entry_price", "sl_price", "tp1", "tp2", "lot", "confidence",
                       "pre_score", "composite_score", "position_in_range", "risk_ratio",
                       "task_composite", "task_latency_ms"):
                if item.get(key) is not None:
                    item[key] = float(item[key])
            if item.get("created_at"):
                item["created_at"] = item["created_at"].isoformat()
            if item.get("updated_at"):
                item["updated_at"] = item["updated_at"].isoformat()
            if item.get("valid_until"):
                item["valid_until"] = item["valid_until"].isoformat()
            if isinstance(item.get("indicator_values"), str):
                import json
                try:
                    item["indicator_values"] = json.loads(item["indicator_values"])
                except (json.JSONDecodeError, TypeError):
                    pass
            if isinstance(item.get("task_indicators"), str):
                import json
                try:
                    item["task_indicators"] = json.loads(item["task_indicators"])
                except (json.JSONDecodeError, TypeError):
                    pass

            return {"code": 0, "data": item, "message": "ok"}

        except Exception as exc:
            logger.error("Signal detail failed for id=%d: %s", signal_id, exc)
            return {"code": "ST_SIG_001", "data": None, "message": str(exc)}

    # ── Signal Stats ────────────────────────────

    @router.get("/stats/summary")
    async def signal_stats(
        symbol: Optional[str] = Query(None),
        hours: int = Query(24, ge=1, le=720),
    ):
        """Get signal statistics summary.

        Args:
            symbol: Filter by symbol.
            hours: Lookback window in hours.
        """
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            symbol_filter = "AND symbol = $2" if symbol else ""
            params: list[Any] = [f"{hours} hours"]
            if symbol:
                params.append(symbol.upper())

            # Total signals
            total_row = await db_pool.fetchrow(
                f"""SELECT COUNT(*) as total
                    FROM hcm_signal.signals
                    WHERE created_at >= now() - $1::interval {symbol_filter}""",
                *params,
            )
            total = total_row["total"] if total_row else 0

            # By direction
            dir_rows = await db_pool.fetch(
                f"""SELECT signal_dir, COUNT(*) as count
                    FROM hcm_signal.signals
                    WHERE created_at >= now() - $1::interval {symbol_filter}
                    GROUP BY signal_dir""",
                *params,
            )
            by_direction = {r["signal_dir"]: r["count"] for r in dir_rows}

            # By regime
            regime_rows = await db_pool.fetch(
                f"""SELECT regime, COUNT(*) as count
                    FROM hcm_signal.signals
                    WHERE created_at >= now() - $1::interval {symbol_filter}
                      AND regime IS NOT NULL
                    GROUP BY regime""",
                *params,
            )
            by_regime = {r["regime"]: r["count"] for r in regime_rows}

            # Average confidence
            avg_row = await db_pool.fetchrow(
                f"""SELECT AVG(confidence) as avg_confidence,
                           AVG(pre_score) as avg_pre_score
                    FROM hcm_signal.signals
                    WHERE created_at >= now() - $1::interval {symbol_filter}
                      AND confidence IS NOT NULL""",
                *params,
            )

            return {
                "code": 0,
                "data": {
                    "total_signals": total,
                    "hours": hours,
                    "symbol": symbol or "all",
                    "by_direction": by_direction,
                    "by_regime": by_regime,
                    "avg_confidence": round(float(avg_row["avg_confidence"] or 0), 4),
                    "avg_pre_score": round(float(avg_row["avg_pre_score"] or 0), 4),
                },
                "message": "ok",
            }

        except Exception as exc:
            logger.error("Signal stats failed: %s", exc)
            return {"code": "ST_SIG_001", "data": None, "message": str(exc)}

    return router
