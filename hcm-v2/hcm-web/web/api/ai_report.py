"""ai_report.py — AI 报表聚合端点（只读，供前端 4 报表页）。

四个端点对应四大报表：
- GET /api/v1/ai/report/health      系统健康监控（事件埋点 + DeepSeek 成功率）
- GET /api/v1/ai/report/layer       信号分层统计（HP候选 vs AI过滤，三模式）
- GET /api/v1/ai/report/performance 交易绩效对比（实盘 vs 影子基准组）
- GET /api/v1/ai/report/snapshot    AI 快照明细（检索复盘）

全部只读聚合 SQL，零写；数据不足时优雅返回空/None（前端显示"数据积累中"）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request

logger = logging.getLogger(__name__)


async def _fetch(db_pool, sql: str, *args):
    if db_pool is None:
        return None
    try:
        return await db_pool.fetch(sql, *args)
    except Exception as exc:
        logger.error("ai_report query failed: %s", exc)
        return None


async def _fetchrow(db_pool, sql: str, *args):
    if db_pool is None:
        return None
    try:
        return await db_pool.fetchrow(sql, *args)
    except Exception as exc:
        logger.error("ai_report query failed: %s", exc)
        return None


def _monitor_redis() -> Any:
    """返回只读 Redis 连接（用于查 monitoring_report.py 写的模型监控报表）。

    monitoring_report.py 是 Windows 主机进程，写的是宿主机 Redis（docker 单实例）。
    web 容器经 REDIS_URL=redis://redis:6379（docker 服务名）可达同一实例，
    故此处优先用 REDIS_URL，回退 localhost:6379（镜像 signal_tower._retrain_redis）。
    失败返回 None（handler 做降级，不抛 500）。
    """
    try:
        import redis
        import os
        url = os.environ.get("REDIS_URL", "")
        if url:
            return redis.from_url(url, socket_timeout=5, decode_responses=True)
        return redis.Redis(host="localhost", port=6379, socket_timeout=5, decode_responses=True)
    except Exception as e:  # pragma: no cover
        logger.warning("monitor redis connect failed: %s", e)
        return None


def create_ai_report_router(db_pool: Any = None, auth_handler: Any = None) -> APIRouter:
    router = APIRouter(tags=["ai-report"])

    # ── ① 系统健康监控 ────────────────────────────────────────────
    async def _health(request: Request, hours: int = 24, user=Depends(auth_handler.require_auth)):
        if db_pool is None:
            return {"code": "SERVICE_NOT_READY", "data": None, "message": "db not available"}
        hours = max(1, min(hours, 24 * 30))
        events = await _fetch(
            db_pool,
            "SELECT event_type, count(*)::int AS cnt FROM hcm_ai.runtime_event "
            "WHERE created_at > now() - make_interval(hours => $1) "
            "GROUP BY event_type ORDER BY 2 DESC",
            hours,
        )
        ds = await _fetchrow(
            db_pool,
            "SELECT count(*) FILTER (WHERE status='ok')::int AS ok_cnt, "
            "count(*) FILTER (WHERE status='fail')::int AS fail_cnt, "
            "count(*) FILTER (WHERE cached)::int AS cache_hit "
            "FROM hcm_ai.ds_output WHERE created_at > now() - make_interval(hours => $1)",
            hours,
        )
        inf = await _fetchrow(
            db_pool,
            "SELECT count(*)::int AS cnt, max(created_at) AS last_ts "
            "FROM hcm_ai.inference_log WHERE created_at > now() - make_interval(hours => $1)",
            hours,
        )
        ev = {r["event_type"]: r["cnt"] for r in (events or [])}
        ok = (ds["ok_cnt"] or 0) if ds else 0
        fail = (ds["fail_cnt"] or 0) if ds else 0
        total_ds = ok + fail
        success_rate = round(ok / total_ds, 4) if total_ds > 0 else None
        data = {
            "window_hours": hours,
            "events": ev,
            "lm_inference_count": (inf["cnt"] or 0) if inf else 0,
            "last_inference_ts": (str(inf["last_ts"]) if inf and inf["last_ts"] else None),
            "ds": {
                "ok": ok,
                "fail": fail,
                "success_rate": success_rate,
                "cache_hit": (ds["cache_hit"] or 0) if ds else 0,
            },
        }
        return {"code": 0, "data": data, "message": "ok"}

    # ── ② 信号分层统计 ────────────────────────────────────────────
    async def _layer(request: Request, days: int = 7, user=Depends(auth_handler.require_auth)):
        if db_pool is None:
            return {"code": "SERVICE_NOT_READY", "data": None, "message": "db not available"}
        days = max(1, min(days, 365))
        hp = await _fetch(
            db_pool,
            "SELECT COALESCE(NULLIF(regime,''),'UNKNOWN') AS regime, count(*)::int AS cnt "
            "FROM hcm_signal.signals WHERE signal_mode LIKE 'HEXP:%' "
            "AND created_at > now() - make_interval(days => $1) "
            "GROUP BY 1 ORDER BY 2 DESC",
            days,
        )
        gate = await _fetch(
            db_pool,
            "SELECT regime, action, count(*)::int AS cnt FROM hcm_ai.gate_decision "
            "WHERE created_at > now() - make_interval(days => $1) "
            "GROUP BY regime, action ORDER BY 1, 2",
            days,
        )
        total_candidates = sum(r["cnt"] for r in (hp or []))
        veto_cnt = sum(r["cnt"] for r in (gate or []) if r["action"] == "VETO")
        data = {
            "window_days": days,
            "candidates": [dict(r) for r in (hp or [])],
            "candidates_total": total_candidates,
            "gate_actions": [dict(r) for r in (gate or [])],
            "filter_rate": round(veto_cnt / total_candidates, 4) if total_candidates > 0 else None,
            "note": "胜率/盈亏比依赖 shadow_eval outcome 与 gate 接入后积累，数据不足时为空",
        }
        return {"code": 0, "data": data, "message": "ok"}

    # ── ③ 交易绩效对比 ────────────────────────────────────────────
    async def _performance(request: Request, days: int = 30, user=Depends(auth_handler.require_auth)):
        if db_pool is None:
            return {"code": "SERVICE_NOT_READY", "data": None, "message": "db not available"}
        days = max(1, min(days, 365))
        live = await _fetchrow(
            db_pool,
            "SELECT count(*)::int AS total, "
            "count(*) FILTER (WHERE profit IS NOT NULL)::int AS closed, "
            "coalesce(sum(profit),0)::float AS sum_profit, "
            "count(*) FILTER (WHERE profit > 0)::int AS win_cnt, "
            "count(*) FILTER (WHERE profit < 0)::int AS loss_cnt "
            "FROM hcm_trading.orders "
            "WHERE open_time > now() - make_interval(days => $1)",
            days,
        )
        # 最大回撤：按平仓时间累计盈亏序列算（仅已平仓）
        pnl_rows = await _fetch(
            db_pool,
            "SELECT profit::float AS p FROM hcm_trading.orders "
            "WHERE profit IS NOT NULL AND close_time IS NOT NULL "
            "AND open_time > now() - make_interval(days => $1) "
            "ORDER BY close_time",
            days,
        )
        base = await _fetchrow(
            db_pool,
            "SELECT count(*)::int AS total, "
            "count(*) FILTER (WHERE outcome='win')::int AS win_cnt, "
            "count(*) FILTER (WHERE outcome='loss')::int AS loss_cnt, "
            "coalesce(avg(pnl_r),0)::float AS avg_pnl_r "
            "FROM hcm_signal.hexp_shadow_eval WHERE outcome IS NOT NULL",
        )
        mdd = 0.0
        peak = 0.0
        cum = 0.0
        for r in (pnl_rows or []):
            cum += r["p"] or 0.0
            peak = max(peak, cum)
            mdd = max(mdd, peak - cum)
        data = {
            "window_days": days,
            "live": dict(live) if live else None,
            "live_max_drawdown": round(mdd, 2),
            "baseline_shadow": dict(base) if base else None,
            "note": "AI增强 vs 纯HP基准：需 orders 增加 ai_enhanced 标记 + shadow_eval 积累后才有严格对比；"
                    "扫损/加仓/提前止盈依赖 close_reason 语义化，当前为 sync_reconcile/null 未区分",
        }
        return {"code": 0, "data": data, "message": "ok"}

    # ── ④ AI 快照明细 ─────────────────────────────────────────────
    async def _snapshot(
        request: Request,
        symbol: str = "",
        min_score: float | None = None,
        max_score: float | None = None,
        limit: int = 100,
        offset: int = 0,
        user=Depends(auth_handler.require_auth),
    ):
        if db_pool is None:
            return {"code": "SERVICE_NOT_READY", "data": None, "message": "db not available"}
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        rows = await _fetch(
            db_pool,
            "SELECT log_id, symbol, ai_score, total_score, ext_factor_score, "
            "sl_coeff, continuity, mode, model_version, created_at "
            "FROM hcm_ai.inference_log "
            "WHERE ($1 = '' OR symbol = $1) "
            "AND ($2::float IS NULL OR ai_score >= $2::float) "
            "AND ($3::float IS NULL OR ai_score <= $3::float) "
            "ORDER BY created_at DESC LIMIT $4 OFFSET $5",
            symbol.upper(),
            min_score,
            max_score,
            limit,
            offset,
        )
        total = await _fetchrow(
            db_pool,
            "SELECT count(*)::int AS cnt FROM hcm_ai.inference_log "
            "WHERE ($1 = '' OR symbol = $1) "
            "AND ($2::float IS NULL OR ai_score >= $2::float) "
            "AND ($3::float IS NULL OR ai_score <= $3::float)",
            symbol.upper(),
            min_score,
            max_score,
        )
        data = {
            "rows": [dict(r) for r in (rows or [])],
            "total": (total["cnt"] or 0) if total else 0,
            "limit": limit,
            "offset": offset,
        }
        return {"code": 0, "data": data, "message": "ok"}

    # ── ⑤ 每日 KPI 聚合（方案 B 日表 + 后台每小时聚合 + AI 真实盈亏贡献）──
    async def _daily(request: Request, days: int = 30, user=Depends(auth_handler.require_auth)):
        if db_pool is None:
            return {"code": "SERVICE_NOT_READY", "data": None, "message": "db not available"}
        days = max(1, min(days, 365))
        rows = await _fetch(
            db_pool,
            "SELECT trade_date, lm_inferences, ds_calls, ds_success, ds_fail, ds_timeout, "
            "cache_hits, fuse_events, degrade_events, hp_candidates, ai_passed, ai_vetoed, "
            "ai_upgraded, ai_downdgraded, ai_opened, total_orders, total_pnl, win_orders, "
            "loss_orders, ai_enhanced_orders, ai_enhanced_pnl, non_ai_pnl, ai_contrib_ratio, "
            "fused_orders, lm_only_orders, ds_only_orders, updated_at "
            "FROM hcm_ai.daily_kpi "
            "WHERE trade_date >= current_date - make_interval(days => $1) "
            "ORDER BY trade_date ASC",
            days,
        )
        data = {
            "days": days,
            "rows": [dict(r) for r in (rows or [])],
        }
        return {"code": 0, "data": data, "message": "ok"}

    # ── ⑥ 模型监控报表（PSI/校准/置信/行情环境，数据真源 Redis）──
    async def _monitor(request: Request, user=Depends(auth_handler.require_auth)):
        """Handler: GET 模型监控报表（P3-A monitoring_report.py 每轮落库的只读展示）。

        数据真源（零 schema 改动，只读 Redis）：
        - hcm:ai:monitor:report:latest  最新一份四视图 JSON（PSI/校准/置信/行情环境）
        - hcm:ai:monitor:report:history 近 50 份列表（趋势对比）

        空键优雅降级：latest=None / history=[]（前端显示"数据积累中"）。
        """
        r = _monitor_redis()
        latest = None
        history = []
        if r is not None:
            try:
                latest_blob = r.get("hcm:ai:monitor:report:latest")
                if latest_blob:
                    latest = json.loads(latest_blob)
                hist_blobs = r.lrange("hcm:ai:monitor:report:history", 0, 49)
                for hb in hist_blobs:
                    try:
                        history.append(json.loads(hb))
                    except Exception:
                        continue
            except Exception as e:
                logger.warning("monitor report read failed: %s", e)
        data = {
            "latest": latest,
            "history": history,
        }
        return {"code": 0, "data": data, "message": "ok"}

    router.add_api_route("/api/v1/ai/report/health", _health, methods=["GET"], summary="系统健康监控")
    router.add_api_route("/api/v1/ai/report/layer", _layer, methods=["GET"], summary="信号分层统计")
    router.add_api_route("/api/v1/ai/report/performance", _performance, methods=["GET"], summary="交易绩效对比")
    router.add_api_route("/api/v1/ai/report/snapshot", _snapshot, methods=["GET"], summary="AI 快照明细")
    router.add_api_route("/api/v1/ai/report/daily", _daily, methods=["GET"], summary="每日 KPI 聚合")
    router.add_api_route("/api/v1/ai/report/monitor", _monitor, methods=["GET"], summary="模型监控报表")
    return router
