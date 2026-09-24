#!/usr/bin/env python3
"""recalibrate_quality.py — 【C 项·2026-09-11】质量头/方向头/买点头在线滚动校准。

依据：docs/设计方案_LightGBM解耦信号级评审与校准治理_20260911.md §校准闭环；
       记忆 27570774（A 侧车 mtime 热重载已落地，B=ai_pred_raw 落库，C=本脚本）。

职责：用 hcm_ai.ai_pred_raw（B 项数据源，sidecar 每 M5 棒落三头 raw 分）
join hcm_signal.signals → build_labels 的 labels.csv 真实标签，
在滚动窗口上重拟合**校准器**（quality/entry 单校准器、direction 三分类字典），
原子替换 ai.lm.*_calib_path 指向的文件 → sidecar 靠 A 项 mtime 指纹热加载生效。

只换校准器、不动模型权重（风险最小）。纪律（与 review_recalibrate 对齐）：
  1. 标签只用 K 线触达标签（labels.csv），不碰 orders 盈亏；
  2. 滚动窗口（--window-days，默认 20）而非累计；
  3. 校准器档位 < ai.lm.recalib_min_levels（默认 8）或单调性 ≤ 0 → 拒绝替换，保留线上；
  4. 原子替换 + .prev 备份 → 可秒级回滚。

用法：
  python recalibrate_quality.py --labels _artifacts/labels.csv --window-days 20 --dry-run
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

DB_URL_DEFAULT = os.environ.get("DB_URL", "postgresql://hcm:hcm_dev_pwd@127.0.0.1:5432/hcm_v2")
REDIS_URL_DEFAULT = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379")
HEALTH_KEY = "hcm:ai:quality:calib_health"
ALERT_KEY = "hcm:ai:quality:calib_alert"
GATE_MIN_LEVELS = int(os.environ.get("QUALITY_RECALIB_MIN_LEVELS", "8"))


# 【2026-09-21 去重】_ece / _monotonicity / _fit_platt 已抽到 _calib_common.py（唯一真源）。
# 以别名 import，保持下方所有调用点（_ece / _monotonicity / _fit_platt）零改动。
# 理由：本脚本与 review_recalibrate.py 此前**各复制了一份逐字节相同**的实现；
#   两条链本应只在"数据源 + 写盘目标"上不同（ai_pred_raw vs review_log；
#   ai.lm.*_calib_path vs models/review_*/calib_review_*.pkl），
#   而**校准质量的评估口径必须一致** —— 否则两个 calib_health 键的差异无法归因
#   （到底是数据差还是实现差）。
from _calib_common import ece as _ece  # noqa: E402
from _calib_common import fit_platt as _fit_platt  # noqa: E402
from _calib_common import monotonicity as _monotonicity  # noqa: E402


def _fit_calibrator(raw: np.ndarray, y: np.ndarray, method: str = "platt") -> NumpyCalibrator:
    """单校准器拟合（与 review_recalibrate._fit_one 同式：platt 默认 / isotonic 回退）。"""
    if method == "isotonic":
        ir = IsotonicRegression(out_of_bounds="clip")
        ir.fit(raw, y)
        return NumpyCalibrator(ir.X_thresholds_, ir.y_thresholds_)
    return _fit_platt(raw, y)


def _fit_quality_grade(head: str, raw: np.ndarray, y: np.ndarray,
                       min_samples: int, method: str) -> dict:
    """拟合 + 质检，返回指标（不写盘）。ok=False 时调用方应保留线上校准器。"""
    out: dict = {"head": head, "n": int(len(raw)), "method": method}
    if len(raw) < min_samples:
        out["skipped"] = f"n<{min_samples}"
        return out
    if len(set(y.tolist())) < 2:
        out["skipped"] = "single-class"
        return out
    cal = _fit_calibrator(raw, y, method)
    levels = int(cal.level_count())
    pc = np.asarray(cal.predict(raw)).ravel()
    out.update({"levels": levels,
                "ece": round(_ece(y, pc), 4),
                "monotonicity": round(_monotonicity(y, pc), 4),
                "brier": round(float(np.mean((pc - y) ** 2)), 4),
                "base_rate": round(float(np.mean(y)), 4)})
    out["ok"] = bool(levels >= GATE_MIN_LEVELS and out["monotonicity"] > 0)
    if not out["ok"]:
        _why = []
        if levels < GATE_MIN_LEVELS:
            _why.append(f"levels={levels}<{GATE_MIN_LEVELS}")
        if out["monotonicity"] <= 0:
            _why.append(f"monotonicity={out['monotonicity']}<=0")
        out["rejected"] = " and ".join(_why) or "unknown"
    return out


def _atomic_replace_single(path: str, cal) -> None:
    """原子替换单校准器 pkl（保留 .prev 备份）。"""
    prev = path + ".prev.pkl"
    if os.path.exists(path):
        with open(path, "rb") as f:
            _old = f.read()
        with open(prev, "wb") as f:
            f.write(_old)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(cal, f)
    os.replace(tmp, path)


def _atomic_replace_dict(path: str, d: dict) -> None:
    """原子替换方向头校准器字典 pkl（结构 {int: NumpyCalibrator}）。"""
    prev = path + ".prev.pkl"
    if os.path.exists(path):
        with open(path, "rb") as f:
            _old = f.read()
        with open(prev, "wb") as f:
            f.write(_old)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(d, f)
    os.replace(tmp, path)


def _load_labels(labels_csv: str) -> pd.DataFrame:
    df = pd.read_csv(labels_csv, usecols=lambda c: c in
                     ("signal_id", "entry_label", "label", "dir_label", "created_at"))
    if "signal_id" not in df.columns:
        raise RuntimeError(f"[fatal] {labels_csv} 缺 signal_id")
    df["signal_id"] = df["signal_id"].astype(str)
    return df.set_index("signal_id")


def _count_pred_raw(db_url: str, window_days: int) -> int:
    """窗口内 ai_pred_raw 原始行数（未 join），用于区分「无原始分」与「无信号配对」。"""
    try:
        conn = psycopg2.connect(db_url, connect_timeout=8)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM hcm_ai.ai_pred_raw "
                            "WHERE t_time >= now() - %s::interval",
                            (f"{int(window_days)} days",))
                return int(cur.fetchone()[0])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return -1


def _load_pred_raw(db_url: str, window_days: int) -> pd.DataFrame:
    """取滚动窗口内三头原始分，并 join signals 得到 signal_id（供 labels 对齐）。

    信号在 M5 棒收盘时产出：raw.t_time(棒开盘)=[s.created_at-5min, s.created_at)，
    故用 created_at 落在 [t_time, t_time+5min) 的半开区间匹配本棒信号。
    """
    conn = psycopg2.connect(db_url, connect_timeout=8)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT r.head, r.raw_proba, r.cal_p, r.pred_class, "
                "       s.signal_id, s.signal_dir "
                "FROM hcm_ai.ai_pred_raw r "
                "JOIN hcm_signal.signals s "
                "  ON s.created_at >= r.t_time "
                " AND s.created_at <  r.t_time + interval '6 minutes' "
                "WHERE r.t_time >= now() - %s::interval "
                "  AND s.signal_dir IN ('BUY','SELL')",
                (f"{int(window_days)} days",),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        return pd.DataFrame(columns=["head", "raw_proba", "cal_p",
                                     "pred_class", "signal_id", "signal_dir"])
    df = pd.DataFrame(rows, columns=["head", "raw_proba", "cal_p",
                                     "pred_class", "signal_id", "signal_dir"])
    # 【D4 2026-09-17 修复·在线校准链永久失败】
    # 本函数直连 PG 取列，`s.signal_id` 是 **bigint ⇒ pandas 推断为 int64**；
    # 而 `_load_labels()`（:152）显式把 labels.csv 的 signal_id 转成 **str**，
    # 于是 :270 的 `raw.join(labels, on="signal_id")` 抛：
    #   ValueError: You are trying to merge on int64 and str columns for key 'signal_id'
    # ⇒ 本脚本**每天 exit=1、从未成功校准过一次**（日志 tools\quality_recalibrate.log 实证），
    #   三头概率长期停留在旧校准器上 ⇒ 概率不可当胜率读。
    # 修法：与 _load_labels 同口径，两侧都归一到 str（bigint→str 无损；不用 int 是因为
    # labels.csv 侧可能含空/非数值行，转 str 更稳）。
    df["signal_id"] = df["signal_id"].astype(str)
    return df


def _load_quality_cfg() -> dict:
    """读 ai.lm.* 解析三头校准器目标路径 + 重校准门槛。

    容器路径 /app/review_models 之类由 scheduler 使用；本脚本在宿主机运行，
    直接用 PG 中配置的原始路径（tools/models/...）。
    """
    out = {"window_days": 20, "min_samples": 300, "method": "platt",
           "min_levels": GATE_MIN_LEVELS, "ece_alert": 0.08,
           "calib_path": None, "dir_calib_path": None, "entry_calib_path": None}
    try:
        with psycopg2.connect(DB_URL_DEFAULT, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT config_key, current_value FROM hcm_config.metadata "
                            "WHERE config_key LIKE 'ai.lm.%'")
                kv = {k: (v or "") for k, v in cur.fetchall()}
        out["window_days"] = int(float(kv.get("ai.lm.recalib_window_days") or 20))
        out["min_samples"] = int(float(kv.get("ai.lm.recalib_min_samples") or 300))
        out["min_levels"] = int(float(kv.get("ai.lm.recalib_min_levels") or GATE_MIN_LEVELS))
        out["ece_alert"] = float(kv.get("ai.lm.recalib_ece_alert") or 0.08)
        out["method"] = (kv.get("ai.lm.recalib_method") or "platt").strip().lower()
        out["calib_path"] = (kv.get("ai.lm.calib_path") or "").strip() or None
        out["dir_calib_path"] = (kv.get("ai.lm.dir_calib_path") or "").strip() or None
        out["entry_calib_path"] = (kv.get("ai.lm.entry_calib_path") or "").strip() or None
    except Exception as e:  # noqa: BLE001
        print(f"[cfg] 读取 ai.lm.* 失败({e}) → 用默认值", file=sys.stderr)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="_artifacts/labels.csv")
    ap.add_argument("--window-days", type=int, default=None)
    ap.add_argument("--min-samples", type=int, default=None)
    ap.add_argument("--method", default=None, choices=["platt", "isotonic"])
    ap.add_argument("--db-url", default=DB_URL_DEFAULT)
    ap.add_argument("--dry-run", action="store_true", help="只评估不写盘")
    args = ap.parse_args()

    cfg = _load_quality_cfg()
    if args.window_days is None:
        args.window_days = cfg["window_days"]
    if args.min_samples is None:
        args.min_samples = cfg["min_samples"]
    _min_levels = int(cfg.get("min_levels", GATE_MIN_LEVELS))
    _method = (args.method or os.environ.get("QUALITY_CALIB_METHOD")
               or cfg["method"]).strip().lower()
    print(f"[cfg] window={args.window_days}d min_samples={args.min_samples} "
          f"min_levels={_min_levels} method={_method} dry={args.dry_run}", file=sys.stderr)

    if not os.path.exists(args.labels):
        print(f"[skip] labels 不存在: {args.labels}", file=sys.stderr)
        return
    labels = _load_labels(args.labels)
    try:
        _labels_age_h = round((time.time() - os.path.getmtime(args.labels)) / 3600.0, 2)
    except Exception:  # noqa: BLE001
        _labels_age_h = None

    _raw_n = _count_pred_raw(args.db_url, args.window_days)
    raw = _load_pred_raw(args.db_url, args.window_days)
    if raw.empty:
        if _raw_n == 0:
            print(f"[skip] ai_pred_raw 窗口({args.window_days}d)内无原始分 —— "
                  f"sidecar 落库(ai.lm.raw_record_enabled)未产生数据")
        else:
            print(f"[skip] ai_pred_raw 有 {_raw_n} 行原始分，但无『BUY/SELL 信号 × 原始分』配对"
                  f"（记录刚起步 / 窗口内无可交易信号），本次不校准")
        return
    merged = raw.join(labels, on="signal_id", how="inner")
    print(f"[data] matched={len(raw)} joined_labels={len(merged)} raw_total={_raw_n} "
          f"window={args.window_days}d")

    report: dict = {"window_days": args.window_days, "joined": int(len(merged)),
                    "min_samples": args.min_samples, "min_levels": _min_levels,
                    "method": _method, "labels_age_hours": _labels_age_h, "heads": {}}

    # ── 质量头：calib_path 单校准器，y = label(0/1) ──
    _q = merged[merged["head"] == "quality"]
    if _q.empty:
        report["heads"]["quality"] = {"head": "quality", "n": 0, "skipped": "no rows"}
    else:
        _sub = _q[["raw_proba", "label"]].dropna()
        _sub = _sub[_sub["label"].isin([0, 1])]
        if _sub.empty:
            report["heads"]["quality"] = {"head": "quality", "n": 0, "skipped": "no valid labels"}
        else:
            _raw = _sub["raw_proba"].astype(float).values
            _y = _sub["label"].astype(int).values
            _g = _fit_quality_grade("quality", _raw, _y, args.min_samples, _method)
            report["heads"]["quality"] = _g
            if _g.get("ok") and cfg["calib_path"] and not args.dry_run:
                try:
                    _atomic_replace_single(cfg["calib_path"],
                                            _fit_calibrator(_raw, _y, _method))
                    report["heads"]["quality"]["replaced"] = True
                except Exception as e:  # noqa: BLE001
                    report["heads"]["quality"]["error"] = str(e)

    # ── 买点头：entry_calib_path 单校准器，y = entry_label(0/1) ──
    _e = merged[merged["head"] == "entry"]
    if _e.empty:
        report["heads"]["entry"] = {"head": "entry", "n": 0, "skipped": "no rows"}
    else:
        _sub = _e[["raw_proba", "entry_label"]].dropna()
        _sub = _sub[_sub["entry_label"].isin([0, 1])]
        if _sub.empty:
            report["heads"]["entry"] = {"head": "entry", "n": 0, "skipped": "no valid labels"}
        else:
            _raw = _sub["raw_proba"].astype(float).values
            _y = _sub["entry_label"].astype(int).values
            _g = _fit_quality_grade("entry", _raw, _y, args.min_samples, _method)
            report["heads"]["entry"] = _g
            if _g.get("ok") and cfg["entry_calib_path"] and not args.dry_run:
                try:
                    _atomic_replace_single(cfg["entry_calib_path"],
                                            _fit_calibrator(_raw, _y, _method))
                    report["heads"]["entry"]["replaced"] = True
                except Exception as e:  # noqa: BLE001
                    report["heads"]["entry"]["error"] = str(e)

    # ── 方向头：dir_calib_path 三分类字典，y = (pred_class == dir_label) ──
    _d = merged[merged["head"] == "direction"]
    if _d.empty:
        report["heads"]["direction"] = {"head": "direction", "n": 0, "skipped": "no rows"}
    else:
        _sub = _d[["raw_proba", "pred_class", "dir_label"]].dropna()
        _sub = _sub[_sub["dir_label"].isin([-1, 0, 1])]
        if _sub.empty:
            report["heads"]["direction"] = {"head": "direction", "n": 0,
                                             "skipped": "no valid labels"}
        else:
            _classes = (-1, 0, 1)
            _d_new = {}
            # 载入现有字典作兜底（某类样本不足时保留线上校准器）
            try:
                if cfg["dir_calib_path"] and os.path.exists(cfg["dir_calib_path"]):
                    with open(cfg["dir_calib_path"], "rb") as f:
                        _d_new = pickle.load(f) or {}
            except Exception:  # noqa: BLE001
                _d_new = {}
            _dir_report = {"head": "direction", "classes": {}}
            for _c in _classes:
                _cs = _sub[_sub["pred_class"] == _c]
                if _cs.empty:
                    _dir_report["classes"][_c] = {"n": 0, "skipped": "no rows"}
                    continue
                _raw = _cs["raw_proba"].astype(float).values
                _y = (_cs["dir_label"].astype(int) == _c).astype(int).values
                _g = _fit_quality_grade(f"direction:{_c}", _raw, _y,
                                        args.min_samples, _method)
                _dir_report["classes"][_c] = _g
                if _g.get("ok"):
                    _d_new[_c] = _fit_calibrator(_raw, _y, _method)
                    if not args.dry_run and cfg["dir_calib_path"]:
                        _dir_report["classes"][_c]["replace"] = True
            report["heads"]["direction"] = _dir_report
            if not args.dry_run and cfg["dir_calib_path"] and any(
                    isinstance(v, dict) and v.get("ok")
                    for v in _dir_report["classes"].values()):
                try:
                    _atomic_replace_dict(cfg["dir_calib_path"], _d_new)
                    report["heads"]["direction"]["replaced"] = True
                except Exception as e:  # noqa: BLE001
                    report["heads"]["direction"]["error"] = str(e)

    # ECE 漂移告警
    _thr = float(cfg.get("ece_alert", 0.08))
    _over = {h: v["ece"] for h, v in report["heads"].items()
             if isinstance(v, dict) and v.get("ece") is not None and v["ece"] > _thr}
    report.update({"ece_alert_threshold": _thr, "alert": bool(_over),
                   "alert_heads": _over})
    if _over:
        print(f"[ALERT] 校准漂移：{_over} 超阈 {_thr}", file=sys.stderr)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    try:
        import redis as _r
        _rc = _r.Redis.from_url(REDIS_URL_DEFAULT, socket_timeout=3)
        _rc.set(HEALTH_KEY, json.dumps(report, ensure_ascii=False), ex=14 * 24 * 3600)
        if _over:
            _rc.set(ALERT_KEY, json.dumps({"threshold": _thr, "heads": _over,
                                            "window_days": args.window_days},
                                           ensure_ascii=False), ex=14 * 24 * 3600)
        else:
            _rc.delete(ALERT_KEY)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 健康指标写 Redis 失败（非致命）: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
