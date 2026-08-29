"""Risk API — Risk control configuration and intercept logs.

Provides:
- GET /api/v1/risk/config — Full risk config (global fields + symbol-level limits)
- PUT /api/v1/risk/config — Batch update global risk config
- GET /api/v1/risk/symbol-limits — All active symbol position/trade limits
- PUT /api/v1/risk/symbol-limits/{symbol} — Update single symbol limit
- GET /api/v1/risk/intercept-logs — Risk intercept logs with pagination
- Legacy aliases: GET/PUT /api/risk/config
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────

# Risk config keys stored in hcm_config.metadata (with "risk." prefix)
RISK_CONFIG_KEYS: list[str] = [
    "risk.max_daily_loss",
    "risk.max_drawdown_percent",
    "risk.max_leverage",
    "risk.kill_switch",
    "risk.max_concurrent_signals",
    "risk.max_open_orders",
    "risk.max_daily_orders",
    "risk.news_filter_enabled",
    "risk.event_risk_mode",
    "risk.auto_close_seconds",
    "risk.max_lot_per_trade",
    "risk.cooldown_minutes",
    "risk.max_total_exposure",
    "risk.max_correlation",
    "risk.stop_out_level",
    "risk.margin_call_level",
    "risk.kill_switch_action",
    "risk.risk_check_interval",
    "risk.lot_base",
    "risk.score_tier_low",
    "risk.lot_multiplier_low",
    "risk.score_tier_mid",
    "risk.lot_multiplier_mid",
    "risk.score_tier_high",
    "risk.lot_multiplier_high",
    # ── 分值驱动最大持仓数（2026-08-11 v2.5）──
    "risk.score_driven_positions_enabled",
    "risk.score_tier_low_positions",
    "risk.score_tier_mid_positions",
    "risk.max_positions_score_low",
    "risk.max_positions_score_mid",
    "risk.max_positions_score_high",
]

# Stripped key names (without "risk." prefix) returned to frontend
RISK_CONFIG_FIELD_NAMES: list[str] = [k.replace("risk.", "") for k in RISK_CONFIG_KEYS]


# ── Pydantic Models ─────────────────────────────

class RiskConfigUpdate(BaseModel):
    """Single risk config update item.

    The 'config_key' is the short key (e.g. 'max_daily_loss').
    The factory prepends 'risk.' internally before writing.
    """
    config_key: str = Field(..., description="Short config key, e.g. 'max_daily_loss'")
    value: str = Field(..., description="New config value (always a string)")


class RiskConfigBatchUpdate(BaseModel):
    """Batch risk config update request."""
    updates: list[RiskConfigUpdate] = Field(..., min_items=1, max_items=100)


class SymbolLimitUpdate(BaseModel):
    """Single symbol position / daily-trade limit update."""
    max_positions: Optional[int] = Field(None, ge=0, description="Max concurrent positions")
    max_daily_trades: Optional[int] = Field(None, ge=0, description="Max daily trades")


class SymbolLimitItem(BaseModel):
    """Aggregated symbol limit for the response."""
    symbol: str = ""
    max_positions: int = 0
    max_daily_trades: int = 0
    cooldown_seconds: int = 0


# ── Helpers ─────────────────────────────────────

def _ensure_int(value: Any, default: int = 0) -> int:
    """Coerce a config value to int, falling back to default."""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


# ── Router Factory ──────────────────────────────

def create_risk_router(
    db_pool: Any = None,
    config_provider: Any = None,
    auth_handler: Any = None,
    redis_client: Any = None,
) -> APIRouter:
    """Create FastAPI router with risk control endpoints.

    Args:
        db_pool: DatabasePool instance (asyncpg wrapper).
        config_provider: ConfigProviderV3 instance.
        auth_handler: AuthHandler instance for RBAC authentication.
        redis_client: RedisClient instance for invalidation broadcasting.

    Returns:
        APIRouter with /api/v1/risk routes and legacy /api/risk aliases.
    """
    router = APIRouter(tags=["risk"])

    # ── Shared handler implementations ──────────

    async def _get_risk_config_impl(request: Request) -> dict:
        """Read full risk configuration via config_provider (Redis → PG).

        Returns a combined response with global_config and symbol_limits.
        """
        try:
            # ── 1. Read global risk config via config_provider ────────
            global_config: dict[str, str] = {}
            if config_provider is not None:
                for key in RISK_CONFIG_KEYS:
                    try:
                        val = await config_provider.get(key, "")
                        field_name = key.replace("risk.", "")
                        global_config[field_name] = val or ""
                    except Exception:
                        global_config[key.replace("risk.", "")] = ""
            else:
                # Fallback to direct PG
                for key in RISK_CONFIG_KEYS:
                    try:
                        row = await db_pool.fetchrow(
                            "SELECT current_value FROM hcm_config.metadata WHERE config_key = $1",
                            key,
                        )
                        field_name = key.replace("risk.", "")
                        global_config[field_name] = row["current_value"] if row else ""
                    except Exception:
                        global_config[key.replace("risk.", "")] = ""

            # ── 2. Aggregate symbol-level limits ───
            # Query all symbol.{SYMBOL}.max_positions keys to discover active symbols
            symbol_rows = await db_pool.fetch(
                """SELECT config_key, current_value
                   FROM hcm_config.metadata
                   WHERE config_key LIKE 'symbol.%max_positions'
                      OR config_key LIKE 'symbol.%max_daily_trades'
                      OR config_key LIKE 'symbol.%cooldown_seconds'
                   ORDER BY config_key"""
            )

            symbol_map: dict[str, dict[str, int]] = {}
            for row in symbol_rows:
                key: str = row["config_key"]
                # Extract symbol from key: "symbol.XAUUSD.max_positions" → "XAUUSD"
                parts = key.split(".", 2)
                if len(parts) < 3:
                    continue
                symbol = parts[1]
                field = parts[2]  # max_positions / max_daily_trades / cooldown_seconds

                if symbol not in symbol_map:
                    symbol_map[symbol] = {
                        "max_positions": 0,
                        "max_daily_trades": 0,
                        "cooldown_seconds": 0,
                    }
                symbol_map[symbol][field] = _ensure_int(row["current_value"])

            symbol_limits = [
                {
                    "symbol": sym,
                    "max_positions": vals["max_positions"],
                    "max_daily_trades": vals["max_daily_trades"],
                    "cooldown_seconds": vals["cooldown_seconds"],
                }
                for sym, vals in sorted(symbol_map.items())
            ]

            return {
                "code": 0,
                "data": {
                    "global_config": global_config,
                    "symbol_limits": symbol_limits,
                },
                "message": "ok",
            }

        except Exception as exc:
            logger.error("Risk config load failed: %s", exc)
            return {"code": "RISK_001", "data": None, "message": str(exc)}

    async def _put_risk_config_impl(body: RiskConfigBatchUpdate) -> dict:
        """Batch update global risk config fields."""
        if config_provider is None:
            return {
                "code": "SERVICE_NOT_READY",
                "data": None,
                "message": "Config provider not available",
            }

        results: list[dict] = []
        success_count = 0
        fail_count = 0

        for update in body.updates:
            try:
                # Prepend "risk." prefix
                full_key = f"risk.{update.config_key}"
                ok = await config_provider.set(full_key, update.value)
                if ok:
                    success_count += 1
                    results.append({"config_key": update.config_key, "status": "ok"})
                    # [2026-07-24 审计加固] 显式双写 Redis hcm:config:v2。
                    # 尽管 config_provider.set 已写 PG + Redis L2，此处再显式 hset 作为即时双写保险，
                    # 确保 signal-tower / mt5_bridge 直接读 Redis L2 时即时生效（不依赖 L2 TTL 回源延迟），
                    # 杜绝"配置中心改了但运行时仍读旧值 / 容器重建后双写丢失"的隐患。
                    #
                    # 【2026-08-28 P1-8】铁律 5.2「空值即未设置」：config_provider.set 已把
                    # 空串/None 归一为 PG current_value=NULL + Redis HDEL。此处若无条件把
                    # update.value 再 hset 回去，空串会被写回 Redis → PG=NULL 而 Redis=""
                    # 永久分裂（面板显示空白、桥侧 float("") 崩溃或静默回退默认）。
                    # 故仅当值为非空字符串时才做这次即时双写保险；空值交由 set() 的 HDEL 处理。
                    try:
                        if (redis_client is not None
                                and update.value is not None
                                and str(update.value).strip() != ""):
                            await redis_client.hset("hcm:config:v2", full_key, update.value)
                        elif update.value is not None and str(update.value).strip() == "":
                            # 双保险：确保空值提交后 Redis 侧确实无残留键（防历史脏值）
                            if redis_client is not None:
                                await redis_client.hdel("hcm:config:v2", full_key)
                    except Exception as rw_exc:
                        logger.debug("redis dual-write failed (non-fatal): %s", rw_exc)
                    # Broadcast invalidation so risk-engine immediately reloads
                    try:
                        if redis_client is not None:
                            await redis_client.publish(
                                "hcm:config:invalidate",
                                json.dumps({"key": full_key, "ts": time.time()}),
                            )
                    except Exception as pub_exc:
                        logger.debug("invalidation publish failed (non-fatal): %s", pub_exc)
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

        logger.info(
            "Risk config batch update: %d/%d succeeded",
            success_count, len(body.updates),
        )

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

    async def _get_symbol_limits_impl(request: Request) -> dict:
        """Get all active symbol position/trade limits."""
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            rows = await db_pool.fetch(
                """SELECT config_key, current_value
                   FROM hcm_config.metadata
                   WHERE config_key LIKE 'symbol.%max_positions'
                      OR config_key LIKE 'symbol.%max_daily_trades'
                      OR config_key LIKE 'symbol.%cooldown_seconds'
                   ORDER BY config_key"""
            )

            symbol_map: dict[str, dict[str, int]] = {}
            for row in rows:
                key: str = row["config_key"]
                parts = key.split(".", 2)
                if len(parts) < 3:
                    continue
                symbol = parts[1]
                field = parts[2]

                if symbol not in symbol_map:
                    symbol_map[symbol] = {
                        "max_positions": 0,
                        "max_daily_trades": 0,
                        "cooldown_seconds": 0,
                    }
                symbol_map[symbol][field] = _ensure_int(row["current_value"])

            items = [
                {
                    "symbol": sym,
                    "max_positions": vals["max_positions"],
                    "max_daily_trades": vals["max_daily_trades"],
                    "cooldown_seconds": vals["cooldown_seconds"],
                }
                for sym, vals in sorted(symbol_map.items())
            ]

            return {
                "code": 0,
                "data": {"items": items, "total": len(items)},
                "message": "ok",
            }

        except Exception as exc:
            logger.error("Symbol limits load failed: %s", exc)
            return {"code": "RISK_001", "data": None, "message": str(exc)}

    async def _put_symbol_limit_impl(symbol: str, body: SymbolLimitUpdate) -> dict:
        """Update position/trade limits for a single symbol."""
        if config_provider is None:
            return {
                "code": "SERVICE_NOT_READY",
                "data": None,
                "message": "Config provider not available",
            }

        updates_made: list[str] = []
        errors: list[str] = []

        if body.max_positions is not None:
            try:
                await config_provider.set(
                    f"symbol.{symbol}.max_positions", str(body.max_positions),
                )
                updates_made.append("max_positions")
            except Exception as exc:
                errors.append(f"max_positions: {exc}")

        if body.max_daily_trades is not None:
            try:
                await config_provider.set(
                    f"symbol.{symbol}.max_daily_trades", str(body.max_daily_trades),
                )
                updates_made.append("max_daily_trades")
            except Exception as exc:
                errors.append(f"max_daily_trades: {exc}")

        if not updates_made and not errors:
            return {
                "code": 0,
                "data": {"symbol": symbol, "updated": []},
                "message": "No fields to update",
            }

        logger.info(
            "Symbol limit updated: symbol=%s, fields=%s", symbol, updates_made,
        )

        return {
            "code": 0,
            "data": {
                "symbol": symbol,
                "updated": updates_made,
                "errors": errors if errors else None,
            },
            "message": f"Updated {len(updates_made)} field(s) for {symbol}",
        }

    async def _get_intercept_logs_impl(
        symbol: Optional[str],
        page: int,
        page_size: int,
    ) -> dict:
        """Query risk intercept logs with optional symbol filter and pagination."""
        if db_pool is None or not db_pool.is_initialized:
            return {"code": "SYS_DB_001", "data": None, "message": "Database not available"}

        try:
            conditions: list[str] = ["1=1"]
            params: list[Any] = []
            param_idx = 1

            if symbol:
                conditions.append(f"symbol = ${param_idx}")
                params.append(symbol)
                param_idx += 1

            where_clause = " AND ".join(conditions)

            # Count total
            count_row = await db_pool.fetchrow(
                f"SELECT COUNT(*) FROM hcm_risk.intercept_logs WHERE {where_clause}",
                *params,
            )
            total = count_row[0] if count_row else 0

            # Fetch page
            offset = (page - 1) * page_size
            rows = await db_pool.fetch(
                f"""SELECT log_id, signal_id, symbol, rule_name, reason, created_at
                    FROM hcm_risk.intercept_logs
                    WHERE {where_clause}
                    ORDER BY created_at DESC
                    LIMIT ${param_idx} OFFSET ${param_idx + 1}""",
                *params, page_size, offset,
            )

            items = []
            for row in rows:
                item = dict(row)
                if item.get("created_at"):
                    item["created_at"] = item["created_at"].isoformat()
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
            logger.error("Intercept logs query failed: %s", exc)
            return {"code": "RISK_001", "data": None, "message": str(exc)}

    # ── API v1 routes ───────────────────────────

    @router.get("/api/v1/risk/config")
    async def get_risk_config(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Get full risk configuration.

        Returns global risk fields (max_daily_loss, max_drawdown_percent, etc.)
        plus aggregated symbol-level position/trade limits.
        """
        return await _get_risk_config_impl(request)

    @router.put("/api/v1/risk/config")
    async def put_risk_config(
        body: RiskConfigBatchUpdate,
        user=Depends(auth_handler.require_auth),
    ):
        """Batch update global risk configuration.

        Accepts a list of {config_key, value} pairs. The 'risk.' prefix is
        added automatically — send short keys like 'max_daily_loss'.
        """
        return await _put_risk_config_impl(body)

    @router.get("/api/v1/risk/symbol-limits")
    async def get_symbol_limits(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """Get all active symbol position and trade limits.

        Aggregates symbol.{SYMBOL}.max_positions, max_daily_trades,
        and cooldown_seconds from hcm_config.metadata.
        """
        return await _get_symbol_limits_impl(request)

    @router.put("/api/v1/risk/symbol-limits/{symbol}")
    async def put_symbol_limit(
        symbol: str,
        body: SymbolLimitUpdate,
        user=Depends(auth_handler.require_auth),
    ):
        """Update position/trade limits for a single symbol.

        Args:
            symbol: Trading symbol, e.g. 'XAUUSD'.
            body: Fields to update (max_positions and/or max_daily_trades).
        """
        return await _put_symbol_limit_impl(symbol, body)

    @router.get("/api/v1/risk/intercept-logs")
    async def get_intercept_logs(
        request: Request,
        symbol: Optional[str] = Query(None, description="Filter by symbol"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        user=Depends(auth_handler.require_auth),
    ):
        """Query risk intercept logs with optional symbol filter and pagination.

        Returns records from hcm_risk.intercept_logs ordered by created_at DESC.
        """
        return await _get_intercept_logs_impl(symbol, page, page_size)

    # ── Legacy backward-compatible routes ───────

    @router.get("/api/risk/config")
    async def legacy_get_risk_config(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Get full risk configuration — alias for /api/v1/risk/config."""
        return await _get_risk_config_impl(request)

    @router.put("/api/risk/config")
    async def legacy_put_risk_config(
        body: RiskConfigBatchUpdate,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] Batch update risk config — alias for /api/v1/risk/config."""
        return await _put_risk_config_impl(body)

    # --- P0 修复：新增 Legacy aliases for symbol-limits & intercept-logs ---

    @router.get("/api/risk/symbol-limits")
    async def legacy_get_symbol_limits(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] alias for /api/v1/risk/symbol-limits."""
        return await _get_symbol_limits_impl(request)

    @router.put("/api/risk/symbol-limits/{symbol}")
    async def legacy_put_symbol_limits(
        symbol: str,
        body: SymbolLimitUpdate,
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] alias for /api/v1/risk/symbol-limits/{symbol}."""
        return await _put_symbol_limit_impl(symbol, body)

    @router.get("/api/risk/intercept-logs")
    async def legacy_get_intercept_logs(
        request: Request,
        symbol: Optional[str] = Query(None, description="Filter by symbol"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        user=Depends(auth_handler.require_auth),
    ):
        """[Legacy] alias for /api/v1/risk/intercept-logs."""
        return await _get_intercept_logs_impl(symbol, page, page_size)

    return router
