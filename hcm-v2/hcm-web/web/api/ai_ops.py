"""AI 中枢监控 — 只读聚合 API（LightGBM 三头运作 + DeepSeek 工作效果）。

【设计定位 2026-08-29】
现有 ai_report.py 承担「离线报表」，本模块补「此刻正在怎么跑」的实时视图：

  GET /api/v1/ai/ops/live/{symbol}
      聚合实时：LightGBM 三头输出 + hexp 方向共振对照 + DeepSeek 票新鲜度
  GET /api/v1/ai/ops/decisions?hours=&limit=
      裁决流水（hcm_ai.gate_decision）+ 动作占比
  GET /api/v1/ai/ops/ds-stats?hours=
      DeepSeek 调用统计（次数/成功率/耗时/缓存命中）+ fake_prob 时序

【铁律合规】
- 全部**只读**：不写配置、不触发任何决策，符合 D 类「不触架构红线」。
- LightGBM 激活判定读 `ai.enabled` + `ai.mode`（PG 真值），
  **不读 `ai.lm.enabled`**（铁律真值附录：该键为幽灵键，运行路径从不读取）。
- 方向共振对照仅做**展示判定**，绝不改写信号方向 —— AI 无独立开仓权
  （铁律 5.1）：hexp 无方向时本模块一律标注「AI 不独立开仓」。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from fastapi import APIRouter, Depends, Request

logger = logging.getLogger(__name__)

# DeepSeek 票陈旧阈值回退值；真值从配置中心 ai.fuse.ds_max_age_sec 读取（禁硬编码）。
DEFAULT_DS_MAX_AGE_SEC = 900.0

# 三头「未启用」展示文案（灰度关闭 / 模型未加载 / 字段缺失统一显示）。
DISABLED_LABEL = "未启用"

# 共振结论语义常量（禁止散落字面量，铁律四·6）
RESONANCE_SAME = "SAME"                    # AI 与 hexp 同向 → 增强
RESONANCE_OPPOSITE = "OPPOSITE"            # AI 与 hexp 反向 → 否决
RESONANCE_HEXP_NONE = "HEXP_NO_DIRECTION"  # hexp 无方向 → AI 不独立开仓
RESONANCE_AI_NONE = "AI_NO_DIRECTION"      # AI 无方向 → 不干预


async def _fetch(db_pool, sql: str, *args):
    if db_pool is None:
        return None
    try:
        return await db_pool.fetch(sql, *args)
    except Exception as exc:
        logger.error("ai_ops query failed: %s", exc)
        return None


async def _fetchrow(db_pool, sql: str, *args):
    if db_pool is None:
        return None
    try:
        return await db_pool.fetchrow(sql, *args)
    except Exception as exc:
        logger.error("ai_ops query failed: %s", exc)
        return None


async def _redis_get(redis_client, key: str) -> dict | None:
    """读 Redis JSON 对象；缺失/解析失败返回 None（handler 降级，不抛 500）。"""
    if redis_client is None or not getattr(redis_client, "is_initialized", False):
        return None
    try:
        raw = await redis_client.get(key)
        if not raw:
            return None
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "ignore")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception as exc:
        logger.warning("ai_ops redis read failed (%s): %s", key, exc)
        return None


async def _cfg_value(config_provider, key: str) -> str | None:
    """读配置真值（PG current_value 为唯一真值，Redis 为 L2 缓存）。"""
    if config_provider is None:
        return None
    try:
        return await config_provider.get(key)
    except Exception:
        return None


def _judge_resonance(hexp_dir: str | None, ai_dir: str | None) -> tuple[str, str]:
    """方向共振判定（**仅展示**，绝不改写方向）。

    铁律 5.1：AI 只有否决权 + 降/升级权，无独立开仓权。故 hexp 无方向时，
    AI 的方向只作参考标注，绝不视为「AI 要开仓」。
    """
    h = (hexp_dir or "").upper()
    a = (ai_dir or "").upper()
    if h not in ("BUY", "SELL"):
        return RESONANCE_HEXP_NONE, "hexp 无方向，AI 不独立开仓"
    if a not in ("BUY", "SELL"):
        return RESONANCE_AI_NONE, "AI 无方向，不干预"
    if h == a:
        return RESONANCE_SAME, "同向增强"
    return RESONANCE_OPPOSITE, "反向否决"


def create_ai_ops_router(
    db_pool: Any = None,
    config_provider: Any = None,
    auth_handler: Any = None,
    redis_client: Any = None,
) -> APIRouter:
    """创建 AI 中枢监控只读路由（factory 函数被 main.py 的 startup() 调用）。"""
    router = APIRouter(tags=["ai-ops"])

    # ── ① 实时聚合：三头 + hexp 共振 + DeepSeek 新鲜度 ──────────────────
    async def _live(
        symbol: str,
        request: Request,
        user=Depends(auth_handler.require_auth),
    ):
        sym = (symbol or "").upper()
        if not sym:
            return {"code": "WB_AIOPS_001", "data": None, "message": "symbol required"}

        ai_snap = await _redis_get(redis_client, f"hcm:live:hexp:ai:{sym}")
        hexp_snap = await _redis_get(redis_client, f"hcm:live:hexp:{sym}")
        ds_out = await _redis_get(redis_client, f"ai:ds:out:{sym}")

        # 配置真值（不读幽灵键 ai.lm.enabled）
        ai_enabled = (await _cfg_value(config_provider, "ai.enabled") or "").strip().lower() == "true"
        ai_mode = (await _cfg_value(config_provider, "ai.mode") or "decoupled").strip().lower()
        try:
            ds_max_age = float(
                (await _cfg_value(config_provider, "ai.fuse.ds_max_age_sec") or "").strip()
                or DEFAULT_DS_MAX_AGE_SEC
            )
        except (TypeError, ValueError):
            ds_max_age = DEFAULT_DS_MAX_AGE_SEC
        ds_key_state = (
            (await _cfg_value(config_provider, "deepseek.api_key") or "").strip()
        )
        # 判定 DeepSeek 是否激活：非空且非占位串（铁律真值附录）
        ds_key_ready = bool(ds_key_state) and ds_key_state != "test_pg_key"

        # ── 三头输出（缺失 → 未启用）──
        heads = {
            "direction": {
                "value": (ai_snap or {}).get("ai_direction"),
                "prob": (ai_snap or {}).get("ai_dir_prob"),
                "enabled": (ai_snap or {}).get("ai_direction") is not None,
                "label": DISABLED_LABEL,
            },
            "entry": {
                "value": (ai_snap or {}).get("ai_entry"),
                "enabled": (ai_snap or {}).get("ai_entry") is not None,
                "label": DISABLED_LABEL,
            },
            "quality": {
                "value": (ai_snap or {}).get("ai_score"),
                "enabled": (ai_snap or {}).get("ai_score") is not None,
                "label": DISABLED_LABEL,
            },
            "state": {
                "value": (ai_snap or {}).get("ai_state"),
                "enabled": (ai_snap or {}).get("ai_state") is not None,
                "label": DISABLED_LABEL,
            },
        }
        for _h in heads.values():
            if _h["enabled"]:
                _h["label"] = None

        # ── 方向共振（仅展示，不改方向）──
        hexp_dir = (hexp_snap or {}).get("direction")
        res_code, res_text = _judge_resonance(hexp_dir, (ai_snap or {}).get("ai_direction"))

        # ── DeepSeek 票新鲜度 ──
        ds_age_sec = None
        ds_stale = True
        ds_state = "no_ticket"
        if isinstance(ds_out, dict) and ds_out.get("ts") is not None:
            try:
                ds_age_sec = max(0.0, time.time() - float(ds_out["ts"]))
                ds_stale = ds_age_sec > ds_max_age
                ds_state = "stale" if ds_stale else "fresh"
            except (TypeError, ValueError):
                ds_age_sec = None

        data = {
            "symbol": sym,
            "lightgbm": {
                "enabled": ai_enabled,
                "mode": ai_mode,
                "active": bool(ai_enabled and ai_mode == "coupled"),
                "status": (ai_snap or {}).get("status"),
                "valid": (ai_snap or {}).get("valid"),
                "model_loaded": (ai_snap or {}).get("model_loaded"),
                "model_version": (ai_snap or {}).get("model_version"),
                "total_score": (ai_snap or {}).get("total_score"),
                "ext_factor_score": (ai_snap or {}).get("ext_factor_score"),
                "degrade_streak": (ai_snap or {}).get("degrade_streak"),
                "snapshot_ts": (ai_snap or {}).get("ts"),
            },
            "heads": heads,
            "hexp": {
                "direction": hexp_dir,
                "grade": (hexp_snap or {}).get("grade"),
                "hp_score": (hexp_snap or {}).get("hp_score"),
                "verdict": (hexp_snap or {}).get("verdict"),
                "scorecard_total": (hexp_snap or {}).get("scorecard_total"),
                "passed": (hexp_snap or {}).get("passed"),
                "close": (hexp_snap or {}).get("close"),
                "atr": (hexp_snap or {}).get("atr"),
            },
            "resonance": {"code": res_code, "text": res_text},
            "deepseek": {
                "key_ready": ds_key_ready,
                "ticket": ds_out,
                "age_sec": round(ds_age_sec, 1) if ds_age_sec is not None else None,
                "max_age_sec": ds_max_age,
                "state": ds_state,
                "stale": ds_stale,
            },
            "feature_health": {
                "missing_ratio": (ai_snap or {}).get("feat_missing_ratio"),
                "constant_ratio": (ai_snap or {}).get("feat_constant_ratio"),
                "outlier_ratio": (ai_snap or {}).get("feat_outlier_ratio"),
                "psi_drift": (ai_snap or {}).get("psi_drift"),
                "max_z": (ai_snap or {}).get("max_z"),
                "drift_level": (ai_snap or {}).get("drift_level"),
            },
            "lm_features": (ai_snap or {}).get("lm_features"),
        }
        return {"code": 0, "data": data, "message": "ok"}

    # ── ② 裁决流水 ────────────────────────────────────────────────────
    async def _decisions(
        request: Request,
        hours: int = 24,
        limit: int = 200,
        symbol: str = "",
        user=Depends(auth_handler.require_auth),
    ):
        if db_pool is None:
            return {"code": "SERVICE_NOT_READY", "data": None, "message": "db not available"}
        hours = max(1, min(int(hours or 24), 24 * 30))
        limit = max(1, min(int(limit or 200), 1000))
        sym = (symbol or "").upper().strip()

        where = "created_at > now() - make_interval(hours => $1)"
        args: list[Any] = [hours]
        if sym:
            where += " AND symbol = $2"
            args.append(sym)

        rows = await _fetch(
            db_pool,
            "SELECT decision_id, symbol, direction, regime, hp_score, c_ai, p, action, "
            "orig_grade, final_grade, lot_tier, total_score, passed, created_at "
            "FROM hcm_ai.gate_decision WHERE " + where + " "
            "ORDER BY created_at DESC LIMIT " + str(limit),
            *args,
        )
        total_row = await _fetchrow(
            db_pool,
            "SELECT count(*)::int AS total, "
            "count(*) FILTER (WHERE action='VETO')::int AS veto_cnt, "
            "count(*) FILTER (WHERE action='DOWNGRADE')::int AS down_cnt, "
            "count(*) FILTER (WHERE action='UPGRADE')::int AS up_cnt, "
            "count(*) FILTER (WHERE action='HOLD')::int AS hold_cnt "
            "FROM hcm_ai.gate_decision WHERE " + where,
            *args,
        )

        stats = {
            "total": int(total_row["total"]) if total_row else 0,
            "veto": int(total_row["veto_cnt"]) if total_row else 0,
            "downgrade": int(total_row["down_cnt"]) if total_row else 0,
            "upgrade": int(total_row["up_cnt"]) if total_row else 0,
            "hold": int(total_row["hold_cnt"]) if total_row else 0,
            "window_hours": hours,
        }
        items = [dict(r) for r in rows] if rows else []
        for it in items:
            for k, v in list(it.items()):
                if hasattr(v, "isoformat"):
                    it[k] = v.isoformat()
        return {"code": 0, "data": {"stats": stats, "items": items}, "message": "ok"}

    # ── ③ DeepSeek 调用统计 ────────────────────────────────────────────
    async def _ds_stats(
        request: Request,
        hours: int = 24,
        symbol: str = "",
        user=Depends(auth_handler.require_auth),
    ):
        if db_pool is None:
            return {"code": "SERVICE_NOT_READY", "data": None, "message": "db not available"}
        hours = max(1, min(int(hours or 24), 24 * 30))
        sym = (symbol or "").upper().strip()

        where = "created_at > now() - make_interval(hours => $1)"
        args: list[Any] = [hours]
        if sym:
            where += " AND symbol = $2"
            args.append(sym)

        row = await _fetchrow(
            db_pool,
            "SELECT count(*)::int AS calls, "
            "count(*) FILTER (WHERE status='ok')::int AS ok_cnt, "
            "count(*) FILTER (WHERE status='fail')::int AS fail_cnt, "
            "count(*) FILTER (WHERE cached)::int AS cache_hit, "
            "round(avg(latency_ms))::int AS avg_latency_ms, "
            "round(avg(fake_prob)::numeric, 4) AS avg_fake_prob, "
            "round(avg(continuity_score)::numeric, 2) AS avg_continuity "
            "FROM hcm_ai.ds_output WHERE " + where,
            *args,
        )
        series_rows = await _fetch(
            db_pool,
            "SELECT date_trunc('hour', created_at) AS bucket, "
            "count(*)::int AS cnt, "
            "round(avg(fake_prob)::numeric, 4) AS avg_fake_prob "
            "FROM hcm_ai.ds_output WHERE " + where + " "
            "GROUP BY 1 ORDER BY 1",
            *args,
        )
        series = []
        for r in series_rows or []:
            b = r["bucket"]
            series.append({
                "bucket": b.isoformat() if hasattr(b, "isoformat") else str(b),
                "cnt": int(r["cnt"]),
                "avg_fake_prob": float(r["avg_fake_prob"]) if r["avg_fake_prob"] is not None else None,
            })

        calls = int(row["calls"]) if row and row["calls"] else 0
        ok_cnt = int(row["ok_cnt"]) if row and row["ok_cnt"] else 0
        cache_hit = int(row["cache_hit"]) if row and row["cache_hit"] else 0
        stats = {
            "calls": calls,
            "ok": ok_cnt,
            "fail": int(row["fail_cnt"]) if row and row["fail_cnt"] else 0,
            "success_rate": round(ok_cnt / calls, 4) if calls else None,
            "cache_hit": cache_hit,
            "cache_hit_rate": round(cache_hit / calls, 4) if calls else None,
            "avg_latency_ms": int(row["avg_latency_ms"]) if row and row["avg_latency_ms"] is not None else None,
            "avg_fake_prob": float(row["avg_fake_prob"]) if row and row["avg_fake_prob"] is not None else None,
            "avg_continuity": float(row["avg_continuity"]) if row and row["avg_continuity"] is not None else None,
            "window_hours": hours,
        }
        return {"code": 0, "data": {"stats": stats, "series": series}, "message": "ok"}

    # ── ④ 自愈中心：三头自愈闭环历史 + 健康状态 ─────────────────────────
    # 数据源（auto_retrain.py 阶段 0~3 落库，零 schema 风险）：
    #   hcm:ai:retrain:last      最新一轮（JSON）
    #   hcm:ai:retrain:history   近 50 轮（LIST，LIFO）
    #   hcm:ai:retrain:fail_streak  连续复活失败计数（决策 5，≥3 告警人工）
    #   hcm:ai:retrain:daemon    守护心跳（TTL 48h）
    # 健康阈值与 auto_retrain.HEAD_METRIC_MIN 一致（决策 1：0.55，可用环境变量覆盖）。
    async def _selfheal(
        request: Request,
        limit: int = 10,
        user=Depends(auth_handler.require_auth),
    ):
        limit = max(1, min(int(limit or 10), 50))
        ret: dict[str, Any] = {"limit": limit, "rounds": [], "health": {}, "daemon": None}

        # 最新一轮
        last = await _redis_get(redis_client, "hcm:ai:retrain:last")
        ret["last"] = last

        # 近 N 轮历史（LRANGE 0..limit-1；RedisClient 封装无 lrange，走 raw() 底层客户端）
        if redis_client is not None and getattr(redis_client, "is_initialized", False):
            try:
                raw_rows = await redis_client.raw.lrange("hcm:ai:retrain:history", 0, limit - 1)
                for raw in raw_rows or []:
                    if isinstance(raw, (bytes, bytearray)):
                        raw = raw.decode("utf-8", "ignore")
                    try:
                        row = json.loads(raw)
                    except (TypeError, ValueError):
                        continue
                    if isinstance(row, dict):
                        ret["rounds"].append(row)
            except Exception as exc:
                logger.warning("ai_ops selfheal history read failed: %s", exc)

        # 失败计数（连续 N 轮复活失败才告警人工）
        fail_streak: int | None = None
        if redis_client is not None and getattr(redis_client, "is_initialized", False):
            try:
                _fs = await redis_client.get("hcm:ai:retrain:fail_streak")
                if isinstance(_fs, (bytes, bytearray)):
                    _fs = _fs.decode("utf-8", "ignore")
                if _fs:
                    fail_streak = int(_fs)
            except (TypeError, ValueError, Exception):
                fail_streak = None
        ret["fail_streak"] = fail_streak

        # 守护心跳
        daemon = await _redis_get(redis_client, "hcm:ai:retrain:daemon")
        ret["daemon"] = daemon

        # ── 健康汇总（以最新一轮为基准；单头退化=立即校准，不阻塞其它头）──
        try:
            head_min = float(os.environ.get("HEAD_METRIC_MIN", "0.55"))
        except (TypeError, ValueError):
            head_min = 0.55
        hh = (last or {}).get("head_health") or {}
        auc = last.get("auc") if isinstance(last, dict) else None
        dir_hit = hh.get("dir_hit")
        entry_auc = hh.get("entry_auc")
        health = {
            "quality_ok": (auc is None) or (auc >= head_min),
            "dir_ok": (dir_hit is None) or (dir_hit >= head_min),
            "entry_ok": (entry_auc is None) or (entry_auc >= head_min),
            "recalib_required": bool(hh.get("recalib_required")),
            "adopted": bool((last or {}).get("adopted")),
            "switched": bool((last or {}).get("switched")),
            "fail_streak_alert": (fail_streak or 0) >= 3,
            "threshold": head_min,
        }
        ret["health"] = health
        ret["rounds_count"] = len(ret["rounds"])
        return {"code": 0, "data": ret, "message": "ok"}

    router.add_api_route(
        "/api/v1/ai/ops/live/{symbol}",
        _live,
        methods=["GET"],
        summary="AI 中枢实时监控：三头输出 + hexp 共振 + DeepSeek 新鲜度",
    )
    router.add_api_route(
        "/api/v1/ai/ops/decisions",
        _decisions,
        methods=["GET"],
        summary="AI 闸门裁决流水与动作占比（只读）",
    )
    router.add_api_route(
        "/api/v1/ai/ops/ds-stats",
        _ds_stats,
        methods=["GET"],
        summary="DeepSeek 调用统计与 fake_prob 时序（只读）",
    )
    router.add_api_route(
        "/api/v1/ai/ops/selfheal",
        _selfheal,
        methods=["GET"],
        summary="AI 自愈中心：三头自愈闭环历史与健康状态（只读）",
    )
    return router
