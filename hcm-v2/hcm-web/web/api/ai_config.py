"""ai_config.py — AI 信号质量模块配置 CRUD（独立命名空间 ai.*）。

提供 GET/PUT 读写所有 ``ai.*`` 配置键，前端 ``AiQualityConfig.tsx`` 通过此端点
渲染四组（lm/ds/cpl/cont + 总开关）。与 cosource.py 同构：白名单 + PATCH 语义，
写经 config_provider.set（PG↔Redis 双写 + PUB 失效广播）。

路由:
- V1 ``GET/PUT /api/v1/ai/config``（OpenAPI schema 可见）
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

logger = logging.getLogger(__name__)

# ── ai.* 白名单 + 默认值（与 0011_ai_quality_config.sql 对齐；禁硬编码）──
AI_KEYS: dict[str, Any] = {
    # 总开关
    "ai.enabled": False,
    "ai.mode": "decoupled",

    # LightGBM 本地评分器（AI_LM）
    # 注意：ai.lm.enabled 是【幽灵键】(B5/B9 修复遗留)，运行路径从不读取——
    # LightGBM 真实激活条件 = ai.enabled=true 且 ai.mode=coupled（见 scheduler._read_ai_quality）。
    # 故此处已移除该白名单键，避免面板渲染成误导性的假开关。
    "ai.lm.model_path": "",
    "ai.lm.calib_path": "",
    "ai.lm.model_version": "v0",
    "ai.lm.pass_threshold": 0.50,
    "ai.lm.down_threshold": 0.60,
    "ai.lm.up_threshold": 0.70,
    "ai.lm.veto_quantile": 0.50,
    "ai.lm.down_quantile": 0.70,
    "ai.lm.up_quantile": 0.85,
    "ai.lm.min_samples_train": 500,
    "ai.lm.retrain_cron": "0 2 * * 1",
    "ai.lm.label_r_win": 1.0,
    "ai.lm.label_r_loss": 1.0,
    "ai.lm.label_horizon_bars": 12,
    "ai.lm.label_sl_atr_fallback": 2.0,
    # 2026-08-18：LightGBM ai_score → SL 宽度缩放（0.8~1.5×ATR，设计文档 ai_sl_coeff 区间）。
    # 方向=分高→宽(1.5)、分低→窄(0.8)；AI 断联/失效回退会话 SL。DeepSeek 不再直接干预订单 SL/TP。
    "ai.lm.sl_scale_enabled": True,
    "ai.lm.sl_scale_min": 0.8,
    "ai.lm.sl_scale_max": 1.5,

    # DeepSeek 异步数据源（AI_DS）
    "ai.ds.enabled": False,
    "ai.ds.timeout_sec": 15.0,
    "ai.ds.cache_ttl_min": 30,
    "ai.ds.sl_coeff_min": 0.8,
    "ai.ds.sl_coeff_max": 1.5,
    "ai.ds.fallback_sl_coeff": 0.0,
    "ai.ds.fallback_continuity": 50,

    # 【2026-08-18 DeepSeek/LightGBM 评分解耦】
    # 原融合权重 ai.fuse.w_lm / ai.fuse.w_ds 已废弃并移除：运行期 c_ai 单源取
    # LightGBM，DeepSeek 不再参与实时融合（其赋能移至离线训练 sample_weight）。
    # ds_max_age_sec 保留：仅判定 DeepSeek 观测票是否陈旧（不影响裁决）。
    "ai.fuse.ds_max_age_sec": 900.0,

    # 耦合闸门（AI_CPL）
    "ai.cpl.enabled": False,
    "ai.cpl.w_trend": 0.7,
    "ai.cpl.w_neutral": 0.6,
    "ai.cpl.w_range": 0.5,
    "ai.cpl.k_trend_min": 1.2,
    "ai.cpl.k_range_max": 0.5,
    "ai.cpl.tier_high": 85.0,
    "ai.cpl.tier_mid": 70.0,
    "ai.cpl.tier_low": 60.0,
    "ai.cpl.lot_high": 1.5,
    "ai.cpl.lot_low": 0.5,

    # 持仓调仓（AI_CONT）
    "ai.cont.enabled": False,
    "ai.cont.strong_min": 70,
    "ai.cont.weak_max": 49,
    "ai.cont.mode": "log",
}


def _coerce(v: Any) -> Any:
    """把默认值按类型回读（bool/float/int/str），避免前端拿到字符串 bool。"""
    for key, default in AI_KEYS.items():
        pass
    if isinstance(v, bool):
        return v
    return v


async def _read_config(config_provider: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, default in AI_KEYS.items():
        raw = await config_provider.get(key)
        if raw is None:
            result[key] = default
            continue
        # 按默认值类型回读
        if isinstance(default, bool):
            result[key] = str(raw).lower() in ("true", "1", "yes", "on")
        elif isinstance(default, int):
            try:
                result[key] = int(float(str(raw)))
            except (TypeError, ValueError):
                result[key] = default
        elif isinstance(default, float):
            try:
                result[key] = float(str(raw))
            except (TypeError, ValueError):
                result[key] = default
        else:
            result[key] = raw
    return result


async def _write_config(config_provider: Any, body: dict) -> dict:
    results: list[dict] = []
    success_count = 0
    fail_count = 0
    skip_count = 0
    for key, value in body.items():
        if key not in AI_KEYS:
            skip_count += 1
            results.append({"key": key, "status": "skipped", "error": "not in whitelist"})
            continue
        new_val = str(value)
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
    return {"results": results, "success": success_count,
            "failed": fail_count, "skipped": skip_count, "total": len(body)}


def create_ai_config_router(
    db_pool: Any = None,
    config_provider: Any = None,
    auth_handler: Any = None,
) -> APIRouter:
    router = APIRouter(tags=["ai"])

    async def _get_config(request: Request, user=Depends(auth_handler.require_auth)):
        if config_provider is None:
            return {"code": "SERVICE_NOT_READY", "data": None, "message": "Config provider not available"}
        try:
            data = await _read_config(config_provider)
            return {"code": 0, "data": data, "message": "ok"}
        except Exception as exc:
            logger.error("AI config read failed: %s", exc)
            return {"code": "WB_AI_001", "data": None, "message": str(exc)}

    async def _put_config(body: dict, request: Request, user=Depends(auth_handler.require_auth)):
        if config_provider is None:
            return {"code": "SERVICE_NOT_READY", "data": None, "message": "Config provider not available"}
        try:
            flat = body.get("updates") if isinstance(body, dict) and isinstance(body.get("updates"), list) else body
            if isinstance(flat, list):
                flat = {it["config_key"]: it.get("value") for it in flat if isinstance(it, dict) and "config_key" in it}
            summary = await _write_config(config_provider, flat)
            code = 0 if summary["failed"] == 0 else "WB_AI_003"
            return {"code": code, "data": summary, "message": f"ok ({summary['success']} written)"}
        except Exception as exc:
            logger.error("AI config write failed: %s", exc)
            return {"code": "WB_AI_002", "data": None, "message": str(exc)}

    router.add_api_route("/api/v1/ai/config", _get_config, methods=["GET"], summary="Read all ai.* configuration keys")
    router.add_api_route("/api/v1/ai/config", _put_config, methods=["PUT"], summary="Batch update ai.* configuration keys")
    return router
