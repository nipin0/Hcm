"""Close (position closing) configuration API — Simple config type.

Provides:
- GET /api/v1/close/config — Read all close.* config items
- PUT /api/v1/close/config — Batch update close config

Backward-compatible aliases:
- GET /api/close/config
- PUT /api/close/config
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

logger = logging.getLogger(__name__)

# ── Config keys (close prefix) with defaults ────

CLOSE_CONFIG_DEFAULTS: dict[str, Any] = {
    "close_method": "auto",
    "trailing_stop_enabled": False,
    "trailing_stop_distance": 0.0,
    "break_even_enabled": False,
    "break_even_trigger": 0,
    "break_even_protect": 0,
    "partial_close_enabled": False,
    "partial_close_ratio": 0.0,
    "close_timeout": 0,
    "breakeven_atr_mult": 0.0,
    "breakeven_tp_ratio": 0.5,  # 保本门槛占 TP 距比例上限（bridge 实际读取）
    # ── 方案乙生效字段（bridge 实际读取，必须进白名单否则 GET 读不回）──
    "trail_wide_atr_mult": 0.5,
    "breakeven_buffer_atr_mult": 0.15,
    # ── 移动止盈封顶比例：trail_start 被 TP 距 × 此比例封顶（下限 0.3 硬编码不可配）。
    # 调高=赢家跑更远才启动追踪；<1 保证固定 TP 先于追踪接管。必须进白名单否则 GET 读不回。──
    "trail_start_tp_ratio": 0.8,
    # tp_atr_multiplier 仍保留，用于显式关闭硬TP（=0）
    "tp_atr_multiplier": 0.0,
    # ── zone SL/TP 偏移（与 bridge place_mt5_order 同读；PG+Redis 同写）──
    "zone_sl_offset_atr_mult": 0.4,
    "zone_tp_offset_atr_mult": 0.3,
    # ── 面板新增（精准入场 + 移动止盈 + 总仓位）──
    "max_sl_atr_mult": 1.8,
    "tp_min_atr_mult": 1.0,
    "tp_max_atr_mult": 6.0,
    "trail_start_atr_mult": 2.0,
    "total_trail_enabled": False,
    "total_trail_start_amount": 30,
    "total_trail_stop_amount": 15,
    "total_trail_check_interval": 10,
    # ── 跟单号每日盈亏熔断（2026-08-11，限额=账户资金百分比）──
    # 跟单桥专属：当日净盈亏占账户资金(余额)的百分比超盈利/亏损上限即熔断，
    # 全平跟单号并停止当日跟单，次日 resume_time 自动恢复（主号完全不受影响）。
    # 上限为百分比数值（如 5.0 = 账户资金的 5%）；0=该方向不限制。
    "follow_circuit_break_enabled": False,  # 总开关
    "follow_daily_profit_max": 0.0,        # 盈利上限（账户资金%，0=不限制盈利方向）
    "follow_daily_loss_max": 0.0,           # 亏损上限（账户资金%，0=不限制亏损方向）
    "follow_resume_time": "06:30",          # 每日恢复跟单时间（本地时区 HH:MM）
    # ── 信号冷却（2026-08-11）：持仓平仓后该品种 N 秒内不再触发新开仓 ──
    # 仅按交易品种（per symbol），主号与跟单号共享同一 Redis 键（任一方平仓即触发双方冷却）；
    # 仅拦截 BUY/SELL 开仓信号，平仓/改仓/部分平仓路径不受影响；half 部分平仓不触发。
    # 0 = 关闭（不写冷却键，闸门恒放行）。
    "after_close_cooldown_sec": 0,
    # ── 同向尾单止损（2026-09-04 用户需求，bridge _check_tail_stop_guard 实际读取）──
    # 同一 (symbol, direction) 组 ≥2 仓 且 ≥1 笔已保本后：最新一笔加仓单(尾单)开仓浮亏
    # > ATR × tail_stop_atr_mult → 全平该同向组，锁定保本单小利，防尾单反转拖垮整体。
    "tail_stop_enabled": True,
    "tail_stop_atr_mult": 0.5,
    "tail_stop_cooldown_sec": 60,
}

# ── 时段感知风险档案 (2026-07-24) ─────────────────────────────────────────────
# 亚盘/欧盘/美盘各自的 SL/TP/追利系数。前端平仓面板按盘口分组可调；
# 桥与信号塔读取 close.<session>.<suffix> 优先于全局 close.<suffix>。
# 默认系数：亚盘低波动→收紧(1.5/1.8)；美盘高波动+数据→放宽且强开追利(2.6, tp_relay)。
SESSIONS = ["asia", "europe", "us"]
SESSION_SUFFIXES = [
    "trailing_stop_distance",      # SL 距离 (ATR 倍数)
    "tp_atr_multiplier",           # TP 距离 (ATR 倍数)
    "min_rr",                      # 最低风险回报比 R:R
    "breakeven_atr_mult",          # 保本触发 ATR 倍数
    "breakeven_tp_ratio",          # 保本门槛占 TP 距比例上限
    "breakeven_buffer_atr_mult",   # 保本地板缓冲 ATR 倍数
    "trail_start_atr_mult",        # 移动止盈启动 ATR 倍数
    "trail_wide_atr_mult",         # 移动止盈线宽 ATR 倍数
    "tp_relay_enabled",            # 移动止盈接力 TP 追利开关
    "tp_trail_wide_atr_mult",      # 承接 TP 追利缓冲 ATR 倍数(TP 跟随现价前移距离)
]
SESSION_DEFAULTS = {
    "asia":   {"trailing_stop_distance": 1.5, "tp_atr_multiplier": 1.8, "min_rr": 1.2,
               "breakeven_atr_mult": 0.5, "breakeven_buffer_atr_mult": 0.15,
               "breakeven_tp_ratio": 0.5, "trail_start_atr_mult": 2.5, "trail_wide_atr_mult": 0.7,
               "tp_relay_enabled": True, "tp_trail_wide_atr_mult": 0.7},
    "europe": {"trailing_stop_distance": 2.0, "tp_atr_multiplier": 2.4, "min_rr": 1.2,
               "breakeven_atr_mult": 0.5, "breakeven_buffer_atr_mult": 0.15,
               "breakeven_tp_ratio": 0.5, "trail_start_atr_mult": 2.0, "trail_wide_atr_mult": 0.7,
               "tp_relay_enabled": True, "tp_trail_wide_atr_mult": 0.7},
    "us":     {"trailing_stop_distance": 2.0, "tp_atr_multiplier": 2.6, "min_rr": 1.3,
               "breakeven_atr_mult": 0.4, "breakeven_buffer_atr_mult": 0.15,
               "breakeven_tp_ratio": 0.5, "trail_start_atr_mult": 1.5, "trail_wide_atr_mult": 0.7,
               "tp_relay_enabled": True, "tp_trail_wide_atr_mult": 0.7},
}

_CLOSE_PREFIX = "close."


def _full_key(friendly: str) -> str:
    """Build the full config key from a friendly short name."""
    return f"{_CLOSE_PREFIX}{friendly}"


async def _read_config(config_provider: Any) -> dict[str, Any]:
    """Read all close.* config values from config_provider."""
    result: dict[str, Any] = {}
    for friendly_key, default in CLOSE_CONFIG_DEFAULTS.items():
        full = _full_key(friendly_key)
        raw = await config_provider.get(full)
        if raw is not None:
            try:
                result[friendly_key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                result[friendly_key] = raw
        else:
            result[friendly_key] = default
    # 时段系数：close.<session>.<suffix> 优先，缺失回退 SESSION_DEFAULTS
    for s in SESSIONS:
        for suf in SESSION_SUFFIXES:
            friendly = f"{s}.{suf}"
            full = f"{_CLOSE_PREFIX}{friendly}"
            raw = await config_provider.get(full)
            if raw is not None:
                try:
                    result[friendly] = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    result[friendly] = raw
            else:
                result[friendly] = SESSION_DEFAULTS[s][suf]
    return result


async def _write_config(config_provider: Any, body: dict) -> dict:
    """Write a dict of friendly_key → value to config_provider via close.* keys."""
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

def create_close_router(
    db_pool: Any = None,
    config_provider: Any = None,
    auth_handler: Any = None,
) -> APIRouter:
    """Create FastAPI router with close config endpoints.

    Args:
        db_pool: DatabasePool instance (unused; kept for factory signature consistency).
        config_provider: ConfigProviderV3 instance.
        auth_handler: AuthHandler instance for RBAC.

    Returns:
        APIRouter with close config routes.
    """
    router = APIRouter(tags=["close"])

    # ── Shared handler implementations ───────────

    async def _get_config(
        request: Request,
        user = Depends(auth_handler.require_auth),
    ):
        """Read all close configuration items."""
        if config_provider is None:
            return {"code": "SERVICE_NOT_READY", "data": None, "message": "Config provider not available"}

        try:
            data = await _read_config(config_provider)
            return {"code": 0, "data": data, "message": "ok"}
        except Exception as exc:
            logger.error("Close config read failed: %s", exc)
            return {"code": "WB_CFG_001", "data": None, "message": str(exc)}

    async def _put_config(
        body: dict,
        request: Request,
        user = Depends(auth_handler.require_auth),
    ):
        """Batch update close configuration."""
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
            logger.error("Close config write failed: %s", exc)
            return {"code": "WB_CFG_002", "data": None, "message": str(exc)}

    # ── V1 routes ────────────────────────────────

    router.add_api_route(
        "/api/v1/close/config",
        _get_config,
        methods=["GET"],
        summary="Get close configuration",
    )
    router.add_api_route(
        "/api/v1/close/config",
        _put_config,
        methods=["PUT"],
        summary="Update close configuration",
    )

    # ── Backward-compatible legacy routes ────────

    router.add_api_route(
        "/api/close/config",
        _get_config,
        methods=["GET"],
        summary="[Legacy] Get close configuration",
        include_in_schema=False,
    )
    router.add_api_route(
        "/api/close/config",
        _put_config,
        methods=["PUT"],
        summary="[Legacy] Update close configuration",
        include_in_schema=False,
    )

    return router
