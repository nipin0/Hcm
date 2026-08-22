#!/usr/bin/env python3
"""monitoring_report.py — P3-A 监控报表落库（只读聚合 hcm_ai.inference_log）。

每轮读取推理日志，聚合 4 类监控视图，结果写 Redis（不改 PG schema / 配置）：
  hcm:ai:monitor:report:latest   (最新一份 JSON)
  hcm:ai:monitor:report:history  (近 50 份列表)
视图：
  1) PSI 滑动窗口     —— 各特征实测分布 vs feature_baseline.json 训练基准(deciles) 的 PSI
  2) 校准分桶         —— ai_score 十分位 × 通过率(passed)，校验高分→高通率单调性
  3) 置信分布         —— ai_score 直方图，检测坍缩/漂移
  4) 行情环境分布     —— direction/grade/mode 占比（注：ai_state 未持久化于 inference_log，
                         需后续在 quality_scorer._persist 增列方可纳入，本轮保持只读不改）

铁律合规：纯 SELECT 读 inference_log + 读 baseline 文件；仅写 Redis 监控键，
不写 PG schema / 不改 ai.lm.* 配置 / 不切模型。

用法：
  python monitoring_report.py --window-hours 24
  python monitoring_report.py --once        # 单次（等价于默认）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _monitor_common import load_baseline, compute_psi_batch, load_live_baseline

DB_URL_DEFAULT = "postgresql://hcm:hcm_dev_pwd@127.0.0.1:5432/hcm_v2"
REDIS_HOST, REDIS_PORT = "127.0.0.1", 6379
REDIS_KEY_LATEST = "hcm:ai:monitor:report:latest"
REDIS_KEY_HISTORY = "hcm:ai:monitor:report:history"


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def _safe_json(x):
    """inference_log 的 JSON 列 psycopg2 已解析为 dict；str 才需 loads。"""
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    try:
        return json.loads(x)
    except Exception:
        return {}


def fetch_rows(conn, window_hours: float):
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ai_score, mode, model_version, features, snapshot "
            "FROM hcm_ai.inference_log WHERE created_at >= %s",
            (since,),
        )
        return cur.fetchall()


def calibration_buckets(ai_scores, passed_list, n_bins: int = 10):
    arr = np.asarray(ai_scores, dtype=float)
    passed = np.asarray(passed_list, dtype=float)
    edges = np.linspace(0, 100, n_bins + 1)
    out = []
    for i in range(n_bins):
        lo, hi = float(edges[i]), float(edges[i + 1])
        m = (arr >= lo) & (arr < hi) if i < n_bins - 1 else (arr >= lo)
        cnt = int(m.sum())
        if cnt == 0:
            out.append({"bin": f"{lo:.0f}-{hi:.0f}", "count": 0,
                        "mean_ai": None, "pass_rate": None})
            continue
        out.append({
            "bin": f"{lo:.0f}-{hi:.0f}", "count": cnt,
            "mean_ai": round(float(arr[m].mean()), 2),
            "pass_rate": round(float(passed[m].mean()), 4),
        })
    return out


def confidence_hist(ai_scores, bin_w: int = 5):
    arr = np.asarray(ai_scores, dtype=float)
    edges = list(range(0, 101, bin_w))
    if edges[-1] != 100:
        edges.append(100)
    counts, _ = np.histogram(arr, bins=edges)
    total = max(1, int(arr.size))
    return [{"bin": f"{edges[i]}-{edges[i + 1]}", "count": int(counts[i]),
             "pct": round(int(counts[i]) / total, 4)} for i in range(len(counts))]


def regime_dist(snaps):
    dir_c, grade_c = Counter(), Counter()
    for s in snaps:
        if not s:
            continue
        if s.get("direction"):
            dir_c[s["direction"]] += 1
        if s.get("grade"):
            grade_c[s["grade"]] += 1
    return {"direction": dict(dir_c), "grade": dict(grade_c)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window-hours", type=float, default=24.0)
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", DB_URL_DEFAULT))
    ap.add_argument("--once", action="store_true", help="单次运行（默认即单次）")
    args = ap.parse_args()

    baseline = load_live_baseline()
    if baseline is None:
        log("[warn] no live baseline; falling back to training baseline")
        baseline = load_baseline()
    feat_cols = baseline.get("features", [])

    import psycopg2
    import redis

    conn = psycopg2.connect(args.db_url, connect_timeout=10)
    try:
        rows = fetch_rows(conn, args.window_hours)
    finally:
        conn.close()
    log(f"fetched {len(rows)} rows (window={args.window_hours}h)")

    if not rows:
        log("[warn] no rows in window; report skipped")
        return

    ai_scores, feat_rows, snaps, passed_list = [], [], [], []
    for ai_score, mode, mv, feats_json, snap_json in rows:
        ai_scores.append(ai_score if ai_score is not None else float("nan"))
        f = _safe_json(feats_json)
        feat_rows.append({c: f.get(c, float("nan")) for c in feat_cols})
        s = _safe_json(snap_json)
        snaps.append(s)
        passed_list.append(1.0 if s.get("passed") else 0.0)

    fdf = pd.DataFrame(feat_rows)

    # 1) PSI 滑动窗口（vs 训练基准 deciles）
    psi = compute_psi_batch(fdf, baseline, feat_cols)

    # 2) 校准分桶
    cal = calibration_buckets(ai_scores, passed_list)

    # 3) 置信分布
    conf = confidence_hist(ai_scores)

    # 4) 行情环境分布（direction/grade；ai_state 未持久化，后续增强）
    reg = regime_dist(snaps)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_hours": args.window_hours,
        "n_samples": len(rows),
        "psi": {
            "max": round(psi["max"], 4),
            "mean": round(psi["mean"], 4),
            "drifted_features": psi["drifted"],
            "per_feature": {k: round(v, 4) for k, v in psi["per_feature"].items()},
        },
        "calibration_buckets": cal,
        "confidence_hist": conf,
        "regime_distribution": reg,
    }

    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, socket_timeout=5, decode_responses=True)
    blob = json.dumps(report, ensure_ascii=False, default=str)
    r.set(REDIS_KEY_LATEST, blob)
    r.lpush(REDIS_KEY_HISTORY, blob)
    r.ltrim(REDIS_KEY_HISTORY, 0, 49)
    log(f"[report] saved -> {REDIS_KEY_LATEST} "
        f"(n={len(rows)} psi_max={psi['max']:.4f} drifted={psi['drifted']})")

    # 控制台摘要
    print(json.dumps(
        {k: report[k] for k in ("n_samples", "psi", "regime_distribution", "confidence_hist")},
        ensure_ascii=False, indent=2, default=str,
    ))


if __name__ == "__main__":
    main()
