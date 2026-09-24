#!/usr/bin/env python3
"""review_recalibrate.py — 【P3 2026-09-11】评审器在线滚动校准（每日 1 次）。

依据：docs/设计方案_LightGBM解耦信号级评审与校准治理_20260911.md §5.1 / §5.2

职责：用 `hcm_ai.review_log` 里记录的**原始分**（extra.entry_raw / quality_raw）
对照 **K 线触达真实标签**（取自 build_labels 的 labels.csv → entry_label / label），
在**滚动窗口**上重拟合 isotonic 校准器。**只换校准器，不动模型权重**（风险最小）。

对照 §5.2「现状失效根因」的四条纪律：
  1. 标签只用 K 线触达（labels.csv），**永不碰 orders 盈亏字段**（曾被 28.3% 伪 0 污染）；
  2. 滚动窗口（`--window-days`，默认 20）而非累计 UNBOUNDED，避免旧脏数据永久稀释；
  3. isotonic 无锚单调拟合；样本 < `--min-samples` 不重校准；
  4. 档位 < 8 或单调性 ≤ 0 → **拒绝替换**（保持线上校准器），仅告警；
  5. 原子替换 + 保留 `.prev` 备份 → 可秒级回滚。

用法：
  python review_recalibrate.py --labels _artifacts/labels.csv \
      --model-dir models/review_staging --window-days 20 --min-samples 300
"""
from __future__ import annotations

import argparse
import json
import os
import pickle
import sys
import time

import numpy as np
import pandas as pd
import psycopg2
from sklearn.isotonic import IsotonicRegression

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from calib_np import NumpyCalibrator, PlattCalibrator  # noqa: E402

DB_URL_DEFAULT = os.environ.get("DB_URL", "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2")
REDIS_URL_DEFAULT = os.environ.get("REDIS_URL", "redis://localhost:6379")
HEALTH_KEY = "hcm:ai:review:calib_health"
# 【§5.3 2026-09-11】ECE 漂移告警独立键（供前端/运维直接消费；无告警时清除）
ALERT_KEY = "hcm:ai:review:calib_alert"
GATE_MIN_LEVELS = int(os.environ.get("REVIEW_RECALIB_MIN_LEVELS", "8"))


# 【2026-09-21 去重】_ece / _monotonicity / _fit_platt 已抽到 _calib_common.py（唯一真源）。
# 以别名 import，保持下方所有调用点（_ece / _monotonicity / _fit_platt）零改动。
# 理由：本脚本与 recalibrate_quality.py 此前**各复制了一份逐字节相同**的实现；
#   两条链本应只在"数据源 + 写盘目标"上不同（hcm_ai.review_log vs hcm_ai.ai_pred_raw），
#   而**校准质量的评估口径必须一致**，否则两个 calib_health 键的差异无法归因。
from _calib_common import ece as _ece  # noqa: E402
from _calib_common import fit_platt as _fit_platt  # noqa: E402
from _calib_common import monotonicity as _monotonicity  # noqa: E402


def _load_true_labels(labels_csv: str) -> pd.DataFrame:
    """K 线触达口径真值（build_labels 产物；**不碰 orders 盈亏**）。"""
    df = pd.read_csv(labels_csv, usecols=lambda c: c in
                     ("signal_id", "entry_label", "label", "dir_label", "created_at"))
    if "signal_id" not in df.columns:
        raise RuntimeError(f"[fatal] {labels_csv} 缺 signal_id")
    return df.set_index("signal_id")


def _load_review_raw(db_url: str, window_days: int) -> pd.DataFrame:
    """取滚动窗口内 review_log 的原始分（extra JSONB）。"""
    conn = psycopg2.connect(db_url, connect_timeout=8)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT signal_id, dir_pred, extra FROM hcm_ai.review_log "
                "WHERE created_at >= now() - %s::interval "
                "  AND signal_id IS NOT NULL AND extra IS NOT NULL",
                (f"{int(window_days)} days",),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    recs = []
    for sid, dpr, extra in rows:
        if not isinstance(extra, dict):
            continue
        recs.append({"signal_id": int(sid),
                     "entry_raw": extra.get("entry_raw"),
                     "quality_raw": extra.get("quality_raw"),
                     # 【§5.3 2026-09-11】方向头监控需要：原始置信 + 预测方向
                     # （真值 dir_label 来自 labels.csv；命中 = dir_pred 映射后 == dir_label）
                     "dir_raw": extra.get("dir_raw"),
                     "dir_pred": dpr})
    return pd.DataFrame(recs)


def _reliability(y: np.ndarray, p: np.ndarray, bins: int = 10) -> list:
    """【§5.3】可靠性表：10 分桶「预测均值 vs 实测胜率 vs 样本数」。

    这是校准健康看板的核心（也是方向头/买点头分层单调性的日常化视图）。
    """
    out = []
    edges = np.linspace(0.0, 1.0, bins + 1)
    for i, (lo, hi) in enumerate(zip(edges[:-1], edges[1:])):
        m = (p >= lo) & (p < hi if hi < 1.0 else p <= hi)
        n = int(m.sum())
        if n == 0:
            continue
        out.append({"bin": i, "lo": round(float(lo), 2), "hi": round(float(hi), 2),
                    "n": n, "pred": round(float(p[m].mean()), 4),
                    "actual": round(float(y[m].mean()), 4)})
    return out


# _fit_platt 已于 2026-09-21 移入 _calib_common.py（见文件上方 import）


def _fit_one(head: str, raw: np.ndarray, y: np.ndarray, model_dir: str,
             min_samples: int, method: str = "platt") -> dict:
    """单头：拟合 → 质检 → 达标才原子替换。返回该头指标。

    【2026-09-11】校准器类型与**离线层保持一致**（platt 默认 / isotonic 回退），
    避免"离线 Platt、在线 isotonic"两套映射互相打架；档位判据统一走 level_count()。
    """
    out: dict = {"head": head, "n": int(len(raw)), "method": method}
    if len(raw) < min_samples:
        out["skipped"] = f"n<{min_samples}"
        return out
    if len(set(y.tolist())) < 2:
        out["skipped"] = "single-class"
        return out
    if method == "isotonic":
        ir = IsotonicRegression(out_of_bounds="clip")
        ir.fit(raw, y)
        cal = NumpyCalibrator(ir.X_thresholds_, ir.y_thresholds_)
    else:
        cal = _fit_platt(raw, y)
    levels = int(cal.level_count())
    pc = np.asarray(cal.predict(raw)).ravel()
    out.update({"levels": levels,
                "ece": round(_ece(y, pc), 4),
                "monotonicity": round(_monotonicity(y, pc), 4),
                "brier": round(float(np.mean((pc - y) ** 2)), 4),
                "base_rate": round(float(np.mean(y)), 4),
                # 【§5.3】10 分桶可靠性表（方向/买点头分层单调性看板）
                "reliability": _reliability(y, pc)})
    out["ok"] = bool(levels >= GATE_MIN_LEVELS and out["monotonicity"] > 0)
    if not out["ok"]:
        # 【2026-09-11 修】原先把两个条件都硬写进原因串，即使其中一个通过也会显示
        # "levels=21(<8)" 这类自相矛盾的误导信息（实测已出现）。现只列真正失败项。
        _why = []
        if levels < GATE_MIN_LEVELS:
            _why.append(f"levels={levels}<{GATE_MIN_LEVELS}")
        if out["monotonicity"] <= 0:
            _why.append(f"monotonicity={out['monotonicity']}<=0")
        out["rejected"] = " and ".join(_why) or "unknown"
        return out
    dst = os.path.join(model_dir, f"calib_review_{head}.pkl")
    prev = os.path.join(model_dir, f"calib_review_{head}.prev.pkl")
    try:
        if os.path.exists(dst):
            with open(dst, "rb") as f:
                _old = f.read()
            with open(prev, "wb") as f:
                f.write(_old)
        tmp = dst + ".tmp"
        with open(tmp, "wb") as f:
            pickle.dump(cal, f)
        os.replace(tmp, dst)          # 原子替换
        out["replaced"] = True
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["replaced"] = False
        out["error"] = str(e)
    return out


def _load_review_cfg() -> dict:
    """读 ai.review.* 配置，解析**实际生效的目标目录**与门槛。

    【2026-09-11 修复 3 个死配置】此前 --model-dir / --window-days / --min-samples
    全是 CLI 硬编码，`ai.review.calib_window_days` / `ai.review.recalib_min_samples`
    无任何消费者；且默认写 `review_staging`（线上读 `review_active`）→ 跑了也不生效，
    还会与"候选区"语义冲突（被下次训练覆盖）。

    现按模式解析：
      · shadow        → ai.review.shadow_model_dir（shadow 运行区，独立于候选区）
      · canary/active → ai.review.model_dir
    容器路径 /app/review_models 映射回宿主 models/（本脚本在宿主机运行）。
    """
    out = {"mode": "shadow", "window_days": 20, "min_samples": 300,
           "model_dir": "models/review_shadow", "method": "platt", "ece_alert": 0.08}
    try:
        import psycopg2
        with psycopg2.connect(DB_URL_DEFAULT, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT config_key, current_value FROM hcm_config.metadata "
                            "WHERE config_key LIKE 'ai.review.%'")
                kv = {k: (v or "") for k, v in cur.fetchall()}
        out["mode"] = (kv.get("ai.review.mode") or "shadow").strip().lower()
        out["window_days"] = int(float(kv.get("ai.review.calib_window_days") or 20))
        out["min_samples"] = int(float(kv.get("ai.review.recalib_min_samples") or 300))
        # 【§5.3】激活 ai.review.ece_alert（此前零引用 = 死配置）
        out["ece_alert"] = float(kv.get("ai.review.ece_alert") or 0.08)
        _md = ((kv.get("ai.review.shadow_model_dir") or "")
               if out["mode"] == "shadow" else "") or (kv.get("ai.review.model_dir") or "")
        if _md:
            out["model_dir"] = _md.replace("/app/review_models", "models")
    except Exception as e:  # noqa: BLE001
        print(f"[cfg] 读取 ai.review.* 失败({e}) → 用默认值", file=sys.stderr)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="_artifacts/labels.csv")
    ap.add_argument("--model-dir", default=None,
                    help="默认按配置解析：shadow→shadow_model_dir；canary/active→model_dir")
    ap.add_argument("--window-days", type=int, default=None, help="默认 ai.review.calib_window_days")
    ap.add_argument("--min-samples", type=int, default=None, help="默认 ai.review.recalib_min_samples")
    ap.add_argument("--method", default=None, choices=["platt", "isotonic"])
    ap.add_argument("--db-url", default=DB_URL_DEFAULT)
    ap.add_argument("--dry-run", action="store_true", help="只评估不写盘")
    args = ap.parse_args()

    # 【2026-09-11】门槛/目录优先取配置（修 3 个死配置），CLI 可覆盖
    cfg = _load_review_cfg()
    if args.model_dir is None:
        args.model_dir = cfg["model_dir"]
    if args.window_days is None:
        args.window_days = cfg["window_days"]
    if args.min_samples is None:
        args.min_samples = cfg["min_samples"]
    _method = (args.method or os.environ.get("REVIEW_CALIB_METHOD")
               or cfg["method"]).strip().lower()
    print(f"[cfg] mode={cfg['mode']} dir={args.model_dir} window={args.window_days}d "
          f"min_samples={args.min_samples} method={_method}", file=sys.stderr)

    if not os.path.exists(args.labels):
        print(f"[skip] labels 不存在: {args.labels}", file=sys.stderr)
        return
    labels = _load_true_labels(args.labels)
    # 【2026-09-11】标签新鲜度入报表：在线校准依赖 labels.csv，而后者由 auto_retrain
    # 每日生成（≈10:0x）。若本任务排在它之前就会**静默**使用前一天的标签（正确性无碍、
    # 时效下降）。写入年龄使时序漂移可观测（计划任务已对齐到 11:00）。
    try:
        _labels_age_h = round((time.time() - os.path.getmtime(args.labels)) / 3600.0, 2)
    except Exception:  # noqa: BLE001
        _labels_age_h = None
    rv = _load_review_raw(args.db_url, args.window_days)
    if rv.empty:
        print("[skip] review_log 窗口内无带 signal_id 的原始分记录"
              "（评审器可能仍处于未启用状态）")
        return
    merged = rv.join(labels, on="signal_id", how="inner")
    print(f"[data] review_rows={len(rv)} joined={len(merged)} window={args.window_days}d")

    os.makedirs(args.model_dir, exist_ok=True)
    report: dict = {"window_days": args.window_days, "joined": int(len(merged)),
                    "min_samples": args.min_samples, "method": _method,
                    "model_dir": args.model_dir,
                    "labels_age_hours": _labels_age_h, "heads": {}}
    for head, raw_col, y_col in (("entry", "entry_raw", "entry_label"),
                                 ("quality", "quality_raw", "label")):
        sub = merged[[raw_col, y_col]].dropna()
        if sub.empty:
            report["heads"][head] = {"head": head, "n": 0, "skipped": "no rows"}
            continue
        raw = sub[raw_col].astype(float).values
        y = sub[y_col].astype(int).values
        if args.dry_run:
            report["heads"][head] = {"head": head, "n": int(len(raw)), "dry_run": True}
            continue
        report["heads"][head] = _fit_one(head, raw, y, args.model_dir,
                                         args.min_samples, _method)

    # 【§5.3 2026-09-11】方向头纳入监控：y = 预测方向是否命中真实方向
    # （与离线层 `_fit_calibrator` 的 multiclass 分支同义：类置信 vs 是否命中）
    if {"dir_raw", "dir_pred", "dir_label"}.issubset(merged.columns):
        _mp = {"BUY": 1, "SELL": -1, "HOLD": 0}
        _sub = merged[["dir_raw", "dir_pred", "dir_label"]].dropna()
        if _sub.empty:
            report["heads"]["direction"] = {"head": "direction", "n": 0,
                                            "skipped": "no rows"}
        else:
            _d_raw = _sub["dir_raw"].astype(float).values
            _d_y = (_sub["dir_pred"].map(_mp).fillna(0).astype(int)
                    == _sub["dir_label"].astype(int)).astype(int).values
            report["heads"]["direction"] = (
                {"head": "direction", "n": int(len(_d_raw)), "dry_run": True}
                if args.dry_run else
                _fit_one("direction", _d_raw, _d_y, args.model_dir,
                         args.min_samples, _method))
    else:
        report["heads"]["direction"] = {"head": "direction", "n": 0,
                                        "skipped": "columns missing"}

    # 【§5.3】ECE 漂移告警：某头 ECE > ai.review.ece_alert → alert=true
    # （激活此前零引用的死配置 ai.review.ece_alert）
    _thr = float(cfg.get("ece_alert", 0.08))
    _over = {h: v["ece"] for h, v in report["heads"].items()
             if isinstance(v, dict) and v.get("ece") is not None and v["ece"] > _thr}
    report.update({"ece_alert_threshold": _thr, "alert": bool(_over),
                   "alert_heads": _over})
    if _over:
        print(f"[ALERT] 校准漂移：{_over} 超阈 {_thr}（近 {args.window_days} 天）",
              file=sys.stderr)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    # 健康指标落 Redis（只读消费；失败仅告警）
    try:
        import redis as _r
        _rc = _r.Redis.from_url(REDIS_URL_DEFAULT, socket_timeout=3)
        _rc.set(HEALTH_KEY, json.dumps(report, ensure_ascii=False), ex=14 * 24 * 3600)
        if _over:                      # 告警独立键
            _rc.set(ALERT_KEY, json.dumps(
                {"threshold": _thr, "heads": _over,
                 "window_days": args.window_days}, ensure_ascii=False),
                ex=14 * 24 * 3600)
        else:                          # 无告警 → 清除，避免陈旧告警常驻
            _rc.delete(ALERT_KEY)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 健康指标写 Redis 失败（非致命）: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
