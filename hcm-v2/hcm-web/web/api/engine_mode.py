"""引擎模式（Engine Mode）配置 API — 读写 ``signal.active_model`` 开关。

【2026-08-28 co_source 清除】
原文件 ``web/api/cosource.py`` 承载两套职责：
  1) 引擎模式切换（``signal.active_model``: manual / hexp / co_source）— **仍在用**
  2) 双源信号引擎的全部参数键白名单（``co.gate.*`` / ``co.v2.*`` / ``co.optuna.*``
     等约 130 个键）— **双源已整体下线，全部成死键**

职责 1 是前端「信号模式」页（Mode.tsx）与 HexpDashboard 引擎状态判定的唯一入口，
不能删除；故本文件仅保留该职责，并更名为 ``engine_mode.py`` 消除 co_source 命名混淆。
原文件所有双源参数键白名单整体移除（对应 PG/Redis 中的键已按空值归一清除）。

路由（V1 可见 + Legacy 隐藏向后兼容）：
  - ``GET/PUT /api/v1/engine-mode/config``
  - ``GET/PUT /api/engine-mode/config``（hidden）
所有写操作经 ``ConfigProviderV3.set()`` 双写 PG(metadata)+Redis(hcm:config:v2)+PUB。
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

logger = logging.getLogger("hcm_web")

# ── 引擎模式键白名单（零硬编码：默认值显式声明于此）──
# signal.active_model: 当前仅两种有效取值 ——
#   "hexp"   : 和乘幂引擎（生产唯一自动信号源）
#   "default": 手动模式（镜像主账户动作）
# 已移除 "co_source"（双源信号引擎 2026-08-28 整体下线）。
ENGINE_MODE_KEYS: dict[str, Any] = {
    "signal.active_model": "default",
}


async def _read_config(config_provider: Any) -> dict[str, Any]:
    """从 ConfigProviderV3 读取引擎模式配置。

    未读到值时回退 ENGINE_MODE_KEYS 默认值（零硬编码：不在代码写死业务值）。
    """
    out: dict[str, Any] = {}
    for key, default in ENGINE_MODE_KEYS.items():
        val = await config_provider.get(key, default)
        out[key] = default if val is None else val
    return out


def _normalize_config_body(body: Any) -> dict:
    """兼容两种调用约定：扁平 dict 与 {updates:[...]}。"""
    if isinstance(body, dict) and "updates" in body:
        flat: dict[str, Any] = {}
        for item in body.get("updates") or []:
            if isinstance(item, dict):
                k = item.get("key") or item.get("config_key")
                if k:
                    flat[k] = item.get("value")
        return flat
    return body if isinstance(body, dict) else {}


async def _write_config(config_provider: Any, body: dict) -> dict:
    """批量写回（空值归一 = 清除该键的覆盖值）。

    白名外侧键（body 中但不在 ENGINE_MODE_KEYS）忽略并报告 skipped。
    """
    summary = {"success": 0, "failed": 0, "skipped": 0, "total": 0}
    for key, value in (body or {}).items():
        summary["total"] += 1
        if key not in ENGINE_MODE_KEYS:
            summary["skipped"] += 1
            logger.warning("engine-mode config write skipped (not in whitelist): %s", key)
            continue
        try:
            await config_provider.set(key, value)
            summary["success"] += 1
        except Exception as exc:
            summary["failed"] += 1
            logger.error("engine-mode config write failed [%s]: %s", key, exc)
    return {
        "success": summary["success"],
        "failed": summary["failed"],
        "skipped": summary["skipped"],
        "total": summary["total"],
    }


# ── Router Factory ──────────────────────────────

def create_engine_mode_router(
    db_pool: Any = None,
    config_provider: Any = None,
    auth_handler: Any = None,
) -> APIRouter:
    """创建引擎模式配置路由（按 hcm-web 约定：factory 函数被 main.py 的 startup() 调用）。"""
    router = APIRouter(tags=["engine-mode"])

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
            logger.error("Engine mode config read failed: %s", exc)
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
            logger.error("Engine mode config write failed: %s", exc)
            return {"code": "WB_CFG_002", "data": None, "message": str(exc)}

    # V1 routes (visible in OpenAPI schema)
    router.add_api_route(
        "/api/v1/engine-mode/config",
        _get_config,
        methods=["GET"],
        summary="Read engine mode configuration (signal.active_model)",
    )
    router.add_api_route(
        "/api/v1/engine-mode/config",
        _put_config,
        methods=["PUT"],
        summary="Update engine mode configuration (signal.active_model)",
    )

    # Legacy backward-compatible routes (hidden from schema)
    router.add_api_route(
        "/api/engine-mode/config",
        _get_config,
        methods=["GET"],
        summary="[Legacy] Read engine mode configuration",
        include_in_schema=False,
    )
    router.add_api_route(
        "/api/engine-mode/config",
        _put_config,
        methods=["PUT"],
        summary="[Legacy] Update engine mode configuration",
        include_in_schema=False,
    )

    return router
