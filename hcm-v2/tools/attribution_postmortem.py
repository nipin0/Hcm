#!/usr/bin/env python3
"""attribution_postmortem.py — 信号亏损语义归因 + 重训建议（A2 事后归因，2026-08-30）。

背景：本工具把「亏损 → 重训」的因果链补全。
  reconcile_labels.py 已把 orders.profit(真实盈亏) JOIN inference_log(AI分/特征/闸门决策)，
  但只做数值报表，不解「为什么亏」。本工具在之上叠一层 DeepSeek 语义归因：
    对每笔亏损信号，重建其完整决策上下文（模型特征 + 质量闸门决策 + 外部因子 +
    特征健康度），交给 LLM 判断亏损主因类别、是否数据质量问题、是否应触发重训，
    并聚合出全局「重训建议」JSON，供 auto_retrain.retrain_once(use_deepseek=True) 消费。

设计纪律（与 reconcile_labels 同构，安全）：
  - 只读 orders / inference_log / macro_snapshots / sentiment_snapshots，不写任何生产表
    （产物为本地 JSON + 可选 stdout；如需落库 attribution_log 用 --db 显式开启）。
  - LLM 仅做语义归因（不生成特征、不替 Isotonic、不实时），符合规范⑦红线。
  - 无 DEEPSEEK_API_KEY 时退化为确定性规则归因，工具仍可用（离线、可复现）。

用法:
  python attribution_postmortem.py --window-days 14
  python attribution_postmortem.py --window-days 14 --out-dir ./attr --db
  python attribution_postmortem.py --window-days 7 --no-llm        # 仅确定性归因
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

import psycopg2

# ── 健壮导入 shared.llm_client（tools 不直连 shared 包，做路径兜底）─────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from shared.llm_client import DeepSeekClient, parse_json_block
except Exception:  # 无 LLM 依赖也能跑确定性退化
    DeepSeekClient = None  # type: ignore
    parse_json_block = None  # type: ignore

DB_URL_DEFAULT = "postgresql://hcm:hcm_dev_pwd@127.0.0.1:5432/hcm_v2"

# 外部因子按 category 分列存储，XAUUSD 对应 metals 类别（与 quality_features.load_env 同口径）
EXT_CATEGORY = "metals"

VALID_CAUSES = {
    "regime_shift", "liquidity_shock", "feature_drift", "label_noise",
    "model_overconfidence", "external_event", "execution", "unknown",
}
VALID_SCOPES = {"full", "incremental", "feature_only", "none"}


def _load_json(col):
    """inference_log 的 JSON 列 psycopg2 可能已解析为 dict，str 才需 loads。"""
    if col is None:
        return None
    if isinstance(col, dict):
        return col
    if isinstance(col, str):
        try:
            return json.loads(col)
        except Exception:
            return None
    return None


def load_losers(conn, window_days: int):
    """亏损订单 JOIN 同信号最近 inference_log（含 features/snapshot）。"""
    win = ""
    if window_days and window_days > 0:
        win = f"AND o.close_time >= now() - interval '{int(window_days)} days'"
    sql = """
        SELECT
            o.signal_id, o.symbol, o.direction, o.profit,
            o.open_time, o.close_time,
            il.ai_score, il.total_score, il.ext_factor_score,
            il.model_version, il.features, il.snapshot
        FROM hcm_trading.orders o
        LEFT JOIN LATERAL (
            SELECT ai_score, total_score, ext_factor_score,
                   model_version, features, snapshot
            FROM hcm_ai.inference_log il
            WHERE il.symbol = o.symbol
              AND il.created_at <= o.close_time
            ORDER BY il.created_at DESC
            LIMIT 1
        ) il ON true
        WHERE o.signal_id IS NOT NULL
          AND o.profit IS NOT NULL AND o.profit < 0
          {win}
        ORDER BY o.close_time
    """.format(win=win)
    with conn.cursor() as cur:
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    return [dict(zip(cols, r)) for r in rows]


def load_external(conn):
    """一次性载入 metals 类别 macro/sentiment 快照，供按时间就近取（训练侧同口径）。

    注：liquidity 当前无持久化快照表（仅 Redis hcm:market:liquidity:score 实时值），
    故历史归因暂缺 liquidity 上下文；待 liquidity_snapshots 表落地后此处补一列即可。
    """
    sql = """
        SELECT category, macro_risk_score, sentiment_risk_score, snapshot_time
        FROM (
            SELECT category, macro_risk_score, NULL::int AS sentiment_risk_score,
                   snapshot_time FROM hcm_market.macro_snapshots
            UNION ALL
            SELECT category, NULL::int, sentiment_risk_score, snapshot_time
            FROM hcm_market.sentiment_snapshots
        ) t
        WHERE category = %s
        ORDER BY snapshot_time
    """
    with conn.cursor() as cur:
        cur.execute(sql, (EXT_CATEGORY,))
        rows = cur.fetchall()
    macro, sent = [], []
    for cat, mr, sr, ts in rows:
        ts = ts if hasattr(ts, "timestamp") else ts
        if mr is not None:
            macro.append((ts, float(mr)))
        if sr is not None:
            sent.append((ts, float(sr)))
    return macro, sent


def _nearest(snaps, ts):
    """返回 snaps[(t, v)] 中 t<=ts 的最近一条；无则全局最新。"""
    if not snaps:
        return None
    best = None
    for t, v in snaps:
        if t <= ts:
            best = v
    return best if best is not None else snaps[-1][1]


# 可解释、对 LLM 友好的特征子集（tmf_* 潜变量不喂原始值，避免前序误判风险）
INTERP_FEATURES = [
    "adx_14", "rsi_14", "macd", "atr_14", "plus_di", "minus_di", "er", "bbw",
    "bbw_pct", "hurst", "mm", "ema20_dist_atr", "body_ratio", "pullback_depth",
    "atr_pct", "spread_num", "spread_atr", "spread_atr_log", "donchian_q",
    "dev_z_ema20", "dev_z_ema60", "dev_z_ema200", "macd_slope3",
    "body_wick_ratio", "extreme_reversal", "di_ratio", "di_net", "close_mom_atr",
    "trend_aligned", "event_proximity_min", "macro_risk_score",
    "sentiment_risk_score", "r_dist_atr", "sl_mult_used", "entry_atr_ratio",
    "ds_fake_prob", "ds_sl_coeff", "ds_continuity",
]


def build_context(row, macro, sent):
    """重建单笔亏损信号的决策上下文，供 LLM 归因。"""
    feats = _load_json(row.get("features")) or {}
    snap = _load_json(row.get("snapshot")) or {}
    ts = row.get("close_time")

    interp = {k: feats.get(k) for k in INTERP_FEATURES if k in feats}
    # 特征健康度（snapshot 已存 feat_*_ratio，直接取）
    health = {
        k: snap.get(k) for k in
        ("feat_missing_ratio", "feat_constant_ratio", "feat_outlier_ratio")
        if snap.get(k) is not None
    }
    ext = {
        "macro_risk_score": _nearest(macro, ts) if macro else None,
        "sentiment_risk_score": _nearest(sent, ts) if sent else None,
        "liquidity": None,  # 待 liquidity_snapshots 表落地
    }
    return {
        "signal_id": row.get("signal_id"),
        "symbol": row.get("symbol"),
        "direction": row.get("direction"),
        "profit_usd": round(float(row.get("profit") or 0.0), 2),
        "gate": {
            "ai_score": row.get("ai_score"),
            "total_score": row.get("total_score"),
            "passed": snap.get("passed"),
            "verdict": snap.get("verdict"),
            "grade": snap.get("grade"),
            "ai_state": snap.get("ai_state"),
            "model_version": row.get("model_version") or snap.get("model_version"),
        },
        "interpretable_features": interp,
        "feature_health": health,
        "external_factors": ext,
        "had_inference_log": feats != {} or snap != {},
    }


def build_prompt(ctx):
    system = (
        "你是黄金 XAUUSD 信号归因分析师。基于一笔亏损信号的完整决策上下文"
        "（模型特征、质量闸门决策、外部因子、特征健康度、真实亏损额），"
        "判断亏损主因类别，并给出是否应触发模型重训的建议。"
        "只输出 JSON，字段固定："
        "loss_cause_category(枚举: regime_shift/liquidity_shock/feature_drift/"
        "label_noise/model_overconfidence/external_event/execution/unknown)、"
        "confidence(0~1)、contributing_factors(字符串数组)、"
        "data_quality_issue(bool)、retrain_recommended(bool)、"
        "retrain_scope(枚举: full/incremental/feature_only/none)、narrative(中文短句)。"
        "不要编造上下文里没有的信息；特征健康度高且无外部冲击时，优先归 unknown 而非臆测。"
    )
    user = json.dumps(ctx, ensure_ascii=False, default=str)
    return system, user


def _deterministic_fallback(ctx):
    """无 LLM 时的规则归因（离线、可复现）。"""
    health = ctx.get("feature_health", {}) or {}
    miss = float(health.get("feat_missing_ratio") or 0.0)
    const = float(health.get("feat_constant_ratio") or 0.0)
    if max(miss, const) > 0.3:
        return {
            "loss_cause_category": "label_noise",
            "confidence": 0.6,
            "contributing_factors": [f"特征健康度差(missing={miss:.2f},constant={const:.2f})"],
            "data_quality_issue": True,
            "retrain_recommended": True,
            "retrain_scope": "feature_only",
            "narrative": "特征缺失/恒值比例偏高，疑似数据质量拖累，建议特征层重训。",
        }
    return {
        "loss_cause_category": "unknown",
        "confidence": 0.3,
        "contributing_factors": [],
        "data_quality_issue": False,
        "retrain_recommended": False,
        "retrain_scope": "none",
        "narrative": "无 LLM 且无可疑特征健康度，无法判定主因（需接 DeepSeek）。",
    }


def _sanitize(attr):
    """把 LLM 输出收敛到契约内，越界值给安全默认。"""
    if not isinstance(attr, dict):
        return None
    cause = attr.get("loss_cause_category")
    if cause not in VALID_CAUSES:
        cause = "unknown"
    scope = attr.get("retrain_scope")
    if scope not in VALID_SCOPES:
        scope = "none"
    try:
        conf = float(attr.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    return {
        "loss_cause_category": cause,
        "confidence": round(conf, 3),
        "contributing_factors": list(attr.get("contributing_factors") or [])[:6],
        "data_quality_issue": bool(attr.get("data_quality_issue", False)),
        "retrain_recommended": bool(attr.get("retrain_recommended", False)),
        "retrain_scope": scope,
        "narrative": str(attr.get("narrative") or "")[:300],
    }


async def attribute_all(rows, macro, sent, use_llm, llm):
    out = []
    for row in rows:
        ctx = build_context(row, macro, sent)
        if use_llm and llm is not None and llm.is_available:
            system, user = build_prompt(ctx)
            try:
                raw = await llm.complete(system, user, max_tokens=700, temperature=0.2)
                attr = _sanitize(parse_json_block(raw) if parse_json_block else None)
            except Exception:
                attr = None
            if attr is None:
                attr = _deterministic_fallback(ctx)
        else:
            attr = _deterministic_fallback(ctx)
        out.append({"signal_id": ctx["signal_id"], "context": ctx, "attribution": attr})
    return out


def aggregate(records):
    """聚合单笔归因为全局重训建议（闭环「亏损→重训」）。"""
    n = len(records)
    if n == 0:
        return {"n_losses": 0, "retrain_recommended": False, "retrain_scope": "none",
                "dominant_cause": None, "category_counts": {}, "data_quality_rate": 0.0}
    cats = {}
    dq = 0
    rec_full = rec_inc = rec_feat = 0
    for r in records:
        a = r["attribution"]
        cats[a["loss_cause_category"]] = cats.get(a["loss_cause_category"], 0) + 1
        if a["data_quality_issue"]:
            dq += 1
        if a["retrain_recommended"]:
            s = a["retrain_scope"]
            if s == "full":
                rec_full += 1
            elif s == "incremental":
                rec_inc += 1
            elif s == "feature_only":
                rec_feat += 1
    dominant = max(cats, key=cats.get)
    dom_rate = cats[dominant] / n
    # 重训决策：>40% 亏损集中于某类主因，或数据质量问题占比>30% → 建议重训
    retrain = (dom_rate >= 0.4) or (dq / n >= 0.3)
    if retrain:
        if rec_full >= rec_inc and rec_full >= rec_feat:
            scope = "full"
        elif rec_feat >= rec_inc:
            scope = "feature_only"
        else:
            scope = "incremental"
    else:
        scope = "none"
    return {
        "n_losses": n,
        "retrain_recommended": retrain,
        "retrain_scope": scope,
        "dominant_cause": dominant,
        "dominant_cause_rate": round(dom_rate, 3),
        "category_counts": cats,
        "data_quality_rate": round(dq / n, 3),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-days", type=int, default=14)
    ap.add_argument("--out-dir", default=".")
    ap.add_argument("--db", action="store_true", help="额外把建议落库 hcm_ai.attribution_log")
    ap.add_argument("--no-llm", action="store_true", help="仅确定性规则归因")
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", DB_URL_DEFAULT))
    args = ap.parse_args()

    llm = DeepSeekClient() if (DeepSeekClient and not args.no_llm) else None
    use_llm = llm is not None and llm.is_available and not args.no_llm

    conn = psycopg2.connect(args.db_url)
    try:
        rows = load_losers(conn, args.window_days)
        if not rows:
            print("[attribution] 窗口内无亏损订单", file=sys.stderr)
            return
        macro, sent = load_external(conn)
        records = asyncio.run(attribute_all(rows, macro, sent, use_llm, llm))

        summary = aggregate(records)
        os.makedirs(args.out_dir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        detail_path = os.path.join(args.out_dir, f"attribution_{stamp}.json")
        rec_path = os.path.join(args.out_dir, "attribution_recommendation.json")
        with open(detail_path, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, default=str, indent=2)
        # 重训建议：单独落一个稳定文件名，供 auto_retrain.retrain_once(use_deepseek=True) 读取
        rec = dict(summary, generated_at=stamp, window_days=args.window_days,
                   llm_used=use_llm)
        with open(rec_path, "w", encoding="utf-8") as f:
            json.dump(rec, f, ensure_ascii=False, indent=2)

        print(f"[attribution] 亏损信号={summary['n_losses']} LLM={use_llm}")
        print(f"[attribution] 主因={summary['dominant_cause']} "
              f"占比={summary.get('dominant_cause_rate')} "
              f"数据质量问题率={summary['data_quality_rate']}")
        print(f"[attribution] 重训建议: retrain={summary['retrain_recommended']} "
              f"scope={summary['retrain_scope']}")
        print(f"[attribution] 明细 -> {detail_path}")
        print(f"[attribution] 重训建议 -> {rec_path}")

        if args.db:
            _persist(conn, rec, records)
    finally:
        conn.close()


def _persist(conn, rec, records):
    """可选落库：建表 if not exists + 写全局建议 + 逐笔归因（显式 --db 才执行）。"""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS hcm_ai.attribution_log ("
                "id SERIAL PRIMARY KEY, generated_at TIMESTAMPTZ, "
                "signal_id TEXT, loss_cause_category TEXT, confidence FLOAT, "
                "data_quality_issue BOOLEAN, retrain_recommended BOOLEAN, "
                "retrain_scope TEXT, narrative TEXT, context_json JSONB)"
            )
            for r in records:
                a = r["attribution"]
                cur.execute(
                    "INSERT INTO hcm_ai.attribution_log "
                    "(generated_at, signal_id, loss_cause_category, confidence, "
                    "data_quality_issue, retrain_recommended, retrain_scope, "
                    "narrative, context_json) VALUES (now(), %s,%s,%s,%s,%s,%s,%s,%s)",
                    (r["signal_id"], a["loss_cause_category"], a["confidence"],
                     a["data_quality_issue"], a["retrain_recommended"],
                     a["retrain_scope"], a["narrative"],
                     json.dumps(r["context"], ensure_ascii=False, default=str)),
                )
        conn.commit()
        print("[attribution] 已落库 hcm_ai.attribution_log")
    except Exception as exc:
        print(f"[attribution] 落库失败(非致命): {exc}", file=sys.stderr)
        try:
            conn.rollback()
        except Exception:
            pass


if __name__ == "__main__":
    main()
