"""Co-Source signal enhancement config API（双源信号增强方案 P1a/P1b 配置 CRUD）.

提供 GET/PUT 读写所有 ``co.*`` 配置键 + ``signal.active_model`` 开关，
前端 ``CoSourceConfig.tsx``（约束 ③）通过此端点渲染 6 组颜色参数面板。

路由:
- V1 ``GET/PUT /api/v1/cosource/config``（OpenAPI schema 可见）
- Legacy ``GET/PUT /api/cosource/config``（向后兼容，hidden）
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════
# 配置白名单 + 默认值（零硬编码：全部 key 从 config_provider 读取；
# 默认值仅在 config_provider 无数据时回退，且对齐 seed SQL 默认值。）
# ═══════════════════════════════════════════════════════════════════

COSOURCE_KEYS: dict[str, Any] = {
    # ── 信号模型开关（约束 ③）──
    "signal.active_model": "default",

    # ── G1 校准因子（蓝色 #3b82f6）──
    "co.calib.pre_trend": 1.0,
    "co.calib.trend": 1.0,
    "co.calib.trend_fade": 1.0,
    "co.calib.range": 1.0,
    "co.calib.neutral": 1.0,
    "co.calib.min_days": 7,

    # ── G2 假信号过滤（橙色 #f97316）──
    # [2026-08-01] 默认值对齐部署真值（Redis hcm:config:v2）：f2/f4/f5 生产实际关闭。
    "co.filter.f1_enabled": True,
    "co.filter.f1_penalty": 20.0,
    "co.filter.f2_enabled": False,
    "co.filter.f2_ratio": 50.0,
    "co.filter.f3_enabled": True,
    "co.filter.f3_minutes": 30,
    "co.filter.f3_penalty": 25.0,
    "co.filter.f4_enabled": False,
    "co.filter.f4_rsi_upper": 70.0,
    "co.filter.f4_rsi_lower": 30.0,
    "co.filter.f5_enabled": False,
    "co.filter.f5_consecutive": 3,
    "co.filter.f5_penalty": 30.0,
    # [2026-07-30 C 组] F6 棒质量 / 点差质量闸门（灰度开关，默认关闭）
    "co.filter.f6_quality_enabled": False,
    "co.filter.f6_quality_min": 0.55,
    "co.filter.f6_spread_q_max": 2.0,

    # ── G3 自适应入市门槛（绿色 #22c55e）──
    # [2026-08-01] 默认值对齐部署真值（Redis hcm:config:v2）：adx_strong 18 / strong.trend 40 /
    # weak.trend 50 / weak.lot 1 / shock.trend 20 / risk.high_offset 5 / risk.med_offset 2 /
    # range.block False（生产实关震荡拦截）。配置键存在时以部署值为准，本字典仅为缺失回退。
    "co.gate.score_scale": 100.0,
    "co.gate.adx_strong": 18.0,
    "co.gate.shock.atr_mult": 2.0,
    "co.gate.range.block": False,
    "co.gate.strong.trend": 40.0,
    "co.gate.with_trend.trend": 30.0,  # [2026-07-31 B] 顺H1方向放宽门槛（前端 CoSourceConfig 已加字段，补齐白名单使面板可读写）
    "co.gate.strong.lot": 1.0,
    "co.gate.strong.sl_atr": 0.5,
    "co.gate.strong.rr_min": 2.0,
    "co.gate.weak.trend": 50.0,
    "co.gate.weak.lot": 1.0,
    "co.gate.weak.sl_atr": 0.6,
    "co.gate.weak.rr_min": 1.5,
    "co.gate.shock.trend": 20.0,
    "co.gate.shock.lot": 0.5,
    "co.gate.shock.sl_atr": 0.7,
    "co.gate.shock.rr_min": 1.5,
    "co.gate.risk.high_offset": 5.0,
    "co.gate.risk.med_offset": 2.0,

    # ── G4 批量 AI（紫色 #a855f7）──
    "co.ai.c1_enabled": True,
    "co.ai.c1_cron": "0 6 * * 1-5",
    "co.ai.c2_enabled": True,
    "co.ai.c2_cron": "0 18 * * 1-5",
    "co.ai.c3_enabled": True,
    "co.ai.c3_cron": "0 12 * * 0",
    "co.ai.emergency_enabled": True,
    "co.ai.emergency_limit": 3,
    "co.ai.max_tokens": 4096,
    "co.ai.temperature": 0.3,

    # ── G5 Optuna 自动调参（青色 #06b6d4）──
    "co.optuna.enabled": True,
    "co.optuna.train_days": 60,
    "co.optuna.test_days": 15,
    "co.optuna.trials": 100,
    "co.optuna.target": "sharpe_ratio",
    "co.optuna.max_drawdown_pct": 15,
    "co.optuna.min_trades": 60,

    # ── G6 执行增强 / FORCE_CLOSE（红色 #ef4444）──
    "co.exec.force_close_enabled": False,  # [2026-08-01] 对齐部署真值（生产实关 FORCE_CLOSE）
    "co.exec.fc_close_mode": "all",
    "co.exec.fc_bar_confirm": 3,
    "co.exec.fc_adx_min": 30,
    "co.exec.position_check_min": 15,

    # ── G3 补充：方向分离最小分（漏暴露，引擎实读 scoring_engine direction_min_score）──
    "co.gate.direction_min_score": 0.30,

    # ── 批量调度 Batch scheduling（原先未进面板，引擎实读 co.batch.*）──
    "co.batch.enabled": True,
    "co.batch.calendar_source": "deepseek_knowledge",
    "co.batch.call1_time": "06:00",
    "co.batch.call2_time": "18:00",
    "co.batch.call3_enabled": True,
    "co.batch.lookback_days": 30,
    "co.batch.timeout_sec": 60,
    "co.batch.emergency.enabled": True,
    "co.batch.emergency.max_per_day": 2,
    "co.batch.emergency.cooldown_h": 2,

    # ── G8 重构方案 v2（精准买点 / 微观态 / 单一门槛 θ）──
    # [2026-08-05] 独立分组：v2 路径全部参数键（去除 apply_v2 硬编码），前端 CoSourceConfig 独立分组渲染。
    # 默认值与 precision_entry.py / micro_state.py / co_source.py 代码默认对齐；经 config_provider.set 双写 PG+Redis。
    "co.v2_enabled": False,
    "co.v2.weight.align": 0.40,
    "co.v2.weight.structure": 0.40,
    "co.v2.weight.rr": 0.20,
    "co.v2.min_rr": 1.2,
    "co.v2.pullback_atr_min": 0.5,
    "co.v2.pullback_atr_max": 1.5,
    "co.v2.theta.TREND_PULLBACK": 0.30,
    "co.v2.theta.TREND_ACCEL": 0.45,
    "co.v2.theta.TREND_EXHAUST": 0.55,
    "co.v2.theta.RANGE": 0.45,
    "co.v2.theta.REVERSAL": 0.50,
    # v2 去除硬编码：F2/F4 折扣与执行参数配置驱动
    "co.v2.filter.f2_discount": 0.15,
    "co.v2.filter.f4_discount": 0.15,
    "co.v2.exec.lot_mult": 1.0,
    "co.v2.exec.sl_atr_mult": 2.0,
    "co.v2.exec.rr_min": 1.2,
    # ── G8 补充：原漏进白名单的 v2 键（前端暴露但 COSOURCE_KEYS 缺失→保存被静默 skip、刷新复原）──
    "co.v2.pullback_block_enabled": True,           # 回踩保护·总开关（co_source.py:149 默认 True）
    "co.v2.pullback_block_depth_atr": 0.0,          # 回踩保护·深度阈值(ATR)（co_source.py:150 默认 0.0）
    "co.v2.accel_strict_enabled": True,             # 加速度严格判定（micro_state.py:151 默认 True）
    "co.v2.accel_lookback": 12,                     # 加速度回看 M5 根数（micro_state.py:152 默认 12）
    "co.v2.accel_er_min": 0.35,                     # 效率比下限（micro_state.py:153 默认 0.35）
    "co.v2.accel_disp_atr_min": 1.0,                # 净位移/ATR 下限（micro_state.py:154 默认 1.0）
    "co.v2.exhaust_probe_enabled": False,           # 衰竭探针（co_source.py:154 默认 False）
    "co.v2.exhaust_probe_lot_mult": 0.4,            # 衰竭探针手数倍率（co_source.py:155 默认 0.4）
    "co.v2.h1_reverse_penalty": 0.30,              # 逆H1折扣（co_source.py:152 默认 0.30）
    "co.v2.theta.TREND_EXHAUST_PROBE": 0.40,       # θ·衰竭探针（precision_entry.py 默认 0.40）
    "co.v2.h1_fallback_enabled": True,             # 盲点修复·H1方向兜底总开关（co_source.py 新增 默认 True）
    "co.v2.h1_fallback_min_strength": 0.50,        # 盲点修复·H1强度门槛（co_source.py 新增 默认 0.50）
    "co.v2.h1_fallback_lot_mult": 0.6,             # 盲点修复·兜底单降仓系数（co_source.py 新增 默认 0.6）
}


async def _read_config(config_provider: Any) -> dict[str, Any]:
    """从 ConfigProviderV3 读取全量 co.* + signal.active_model 配置。

    未读到值时回退 COSOURCE_KEYS 默认值（零硬编码：不在代码写死业务值）。
    """
    result: dict[str, Any] = {}
    for key, default in COSOURCE_KEYS.items():
        raw = await config_provider.get(key)
        if raw is not None:
            try:
                result[key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                result[key] = raw
        else:
            result[key] = default
    return result


def _normalize_config_body(body: Any) -> dict:
    """把前端两种传参约定统一成扁平 dict {key: value}。

    - updates 列表: {"updates": [{"config_key": k, "value": v}, ...]}
      （CoSourceConfig.tsx 与站点惯例一致，曾因后端只认扁平 dict 导致批量保存静默 no-op）
    - 扁平 dict: {"co.filter.f2_enabled": "true", ...}
      （Mode.tsx 切换 signal.active_model 使用）
    """
    if not isinstance(body, dict):
        return {}
    updates = body.get("updates")
    if isinstance(updates, list):
        flat: dict = {}
        for item in updates:
            if isinstance(item, dict) and "config_key" in item:
                flat[item["config_key"]] = item.get("value")
        return flat
    return body


async def _write_config(config_provider: Any, body: dict) -> dict:
    """逐键写入 ConfigProviderV3（自动 PG→Redis 双写 + PUB 失效广播）。

    PATCH 语义：写前比对当前存储值，未变化的键直接跳过（status=unchanged），
    避免前端整表 PUT 把 UI 之外手动管理的键（如 co.gate.range.block）覆盖回默认。
    白名外侧键（body 中但不在 COSOURCE_KEYS）忽略并报告 skipped。
    """
    results: list[dict] = []
    success_count = 0
    fail_count = 0
    skip_count = 0

    for key, value in body.items():
        if key not in COSOURCE_KEYS:
            skip_count += 1
            results.append({"key": key, "status": "skipped", "error": "not in whitelist"})
            continue
        new_val = str(value)
        # PATCH：值未变则跳过，防止整表 PUT 覆盖手动管理键
        try:
            current = await config_provider.get(key)
        except Exception:
            current = None
        if current is not None and current == new_val:
            skip_count += 1
            results.append({"key": key, "status": "unchanged"})
            continue
        try:
            ok = await config_provider.set(key, new_val)
            if ok:
                success_count += 1
                results.append({"key": key, "status": "ok"})
            else:
                fail_count += 1
                results.append({"key": key, "status": "failed"})
        except Exception as exc:
            fail_count += 1
            results.append({"key": key, "status": "error", "error": str(exc)})

    return {
        "results": results,
        "success": success_count,
        "failed": fail_count,
        "skipped": skip_count,
        "total": len(body),
    }


# ── Router Factory ──────────────────────────────

def create_cosource_router(
    db_pool: Any = None,
    config_provider: Any = None,
    auth_handler: Any = None,
) -> APIRouter:
    """创建共源信号配置路由（按 hcm-web 约定：factory 函数被 main.py 的 startup() 调用）。"""
    router = APIRouter(tags=["cosource"])

    async def _get_config(
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        if config_provider is None:
            return {
                "code": "SERVICE_NOT_READY",
                "data": None,
                "message": "Config provider not available",
            }
        try:
            data = await _read_config(config_provider)
            return {"code": 0, "data": data, "message": "ok"}
        except Exception as exc:
            logger.error("CoSource config read failed: %s", exc)
            return {"code": "WB_CFG_001", "data": None, "message": str(exc)}

    async def _put_config(
        body: dict,
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        if config_provider is None:
            return {
                "code": "SERVICE_NOT_READY",
                "data": None,
                "message": "Config provider not available",
            }
        try:
            # 兼容两种调用约定：扁平 dict（Mode.tsx）与 {updates:[...]}（CoSourceConfig.tsx）
            flat_body = _normalize_config_body(body)
            summary = await _write_config(config_provider, flat_body)
            code = 0 if summary["failed"] == 0 else "WB_CFG_003"
            msg = (
                f"ok ({summary['success']} written, {summary['skipped']} skipped)"
                if summary["failed"] == 0
                else f"partial ({summary['success']}/{summary['total']} succeeded, {summary['failed']} failed)"
            )
            return {"code": code, "data": summary, "message": msg}
        except Exception as exc:
            logger.error("CoSource config write failed: %s", exc)
            return {"code": "WB_CFG_002", "data": None, "message": str(exc)}

    # V1 routes (visible in OpenAPI schema)
    router.add_api_route(
        "/api/v1/cosource/config",
        _get_config,
        methods=["GET"],
        summary="Read all co-source configuration keys",
    )
    router.add_api_route(
        "/api/v1/cosource/config",
        _put_config,
        methods=["PUT"],
        summary="Batch update co-source configuration keys",
    )

    # Legacy backward-compatible routes (hidden from schema)
    router.add_api_route(
        "/api/cosource/config",
        _get_config,
        methods=["GET"],
        summary="[Legacy] Read all co-source configuration keys",
        include_in_schema=False,
    )
    router.add_api_route(
        "/api/cosource/config",
        _put_config,
        methods=["PUT"],
        summary="[Legacy] Batch update co-source configuration keys",
        include_in_schema=False,
    )

    return router
