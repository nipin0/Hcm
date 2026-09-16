#!/usr/bin/env python3
"""calib_state_labels.py — 4 类状态标签阈值标定（方案 §2.4 第 2 步：网格搜索）。

只读 PG。度量只算一次，随后向量化穷举阈值组合 → 按「**最小类占比最大化**、
`ambiguous ≤ --max-amb`」排序输出，供选定阈值写回配置中心。

【2026-09-15 从 `_scratch/_tmp_state_calib.py` 提升为正式工具】
依据方案 §17.5.3-3：该工具"有长期价值，建议提升为正式测试/工具位置"。
本文件是**唯一**的阈值标定实现；`_scratch` 中同名脚本为历史副本，不再维护。

【为什么把网格从命令行而不是写死】§14.2/§16.1 的阈值是在**被污染**的
ATR 口径下标定出来的（见方案 §36/§50/§51）。地基洁净后度量分布整体移动
（实测：`disp_osc` 的 p25 由 0.25 量级升到 **0.60**、`adx_slope_min` 的 |p10| 到 **10.9**），
旧网格上界（`disp_osc ≤ 0.35`）会被最优解**撞满**，说明搜索空间被截断。
故 `--grid wide` 覆盖了分位建议值；**撞边界时必须换 wide 重跑**。

⚠️ 本脚本内 `classify` 是**为速度做的向量化镜像**（生产口径在
`build_state_labels.classify_label`）。内置 `_consistency_check()` 随机抽样逐条
比对两者结论，不一致即报错退出 —— 防止镜像与生产口径漂移（本项目红线）。

⚠️ **目标函数的局限（诚实标注）**：§2.4 要求的网格目标函数是
"4 类可分性（宏平均 F1 / AUC-OVR）+ 时序 CV"，而本脚本用的是**类别平衡度**代理
（最小类占比）。原因：F1/AUC 需在**每个组合上训练一次模型**，代价不可接受。
因此本脚本的产出是**候选阈值**，最终取舍必须由"用该阈值重产标签 → 训练 → 比指标"
（A/B）决定，不可仅凭类别平衡度就写回生产。

用法：
  # 旧网格（复现 §14.2/§16.1）
  python calib_state_labels.py --symbol XAUUSD --tf M5 --grid narrow
  # 宽网格（洁净数据；撞边界时必用）
  python calib_state_labels.py --symbol XAUUSD --tf M5 --grid wide --json-out _scratch/calib_wide.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from itertools import product

import numpy as np
import pandas as pd
import psycopg2

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_FEAT = os.path.join(_ROOT, "hcm-signal-tower", "signal_tower", "state_features.py")
_BLD = os.path.join(_HERE, "build_state_labels.py")

DB_URL = os.environ.get("DB_URL", "postgresql://hcm:hcm_dev_pwd@localhost:5432/hcm_v2")
NAMES = ["oscillation", "trend_init", "trend_mid", "trend_fade"]

# 固定不变的阈值（§2.4 的"置信带"与 ADX 主阈；不参与网格）
BASE = dict(adx_trend=22.0, er_band=0.015, disp_band=0.04, adx_band=1.0)

# 网格预设。`narrow` = §14.2/§16.1 使用的原始网格（保留以复现历史结论，勿删）。
# `wide` = 覆盖洁净数据分位建议值（p25/p45/|p10| 实测：0.60 / 1.18 / 10.9）。
GRIDS: dict[str, dict[str, tuple]] = {
    "narrow": {
        "er_osc": (0.15, 0.20, 0.25),
        "disp_osc": (0.25, 0.35),
        "er_trend": (0.30, 0.35),
        "disp_min": (0.30, 0.40),
        "er_fade": (0.30, 0.35),
        "disp_init": (0.35, 0.45),
        "fade_ret_atr": (0.60, 0.80),
        "adx_slope_min": (3.0, 5.0),
    },
    "wide": {
        "er_osc": (0.15, 0.20, 0.25, 0.30),
        "disp_osc": (0.25, 0.40, 0.55, 0.70),
        "er_trend": (0.30, 0.35, 0.40),
        "disp_min": (0.30, 0.60, 0.90, 1.20),
        "er_fade": (0.20, 0.30, 0.35),
        "disp_init": (0.35, 0.45, 0.55),
        "fade_ret_atr": (0.48, 0.60, 0.80),
        "adx_slope_min": (3.0, 5.0, 8.0, 11.0),
    },
}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SF = _load("state_features", _FEAT)
B = _load("build_state_labels", _BLD)


def load(symbol, tf):
    conn = psycopg2.connect(DB_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT open_time, high, low, close FROM hcm_market.klines "
                        "WHERE symbol=%s AND time_frame=%s ORDER BY open_time",
                        (symbol, tf))
            rows = cur.fetchall()
    finally:
        conn.close()
    df = pd.DataFrame(rows, columns=["open_time", "high", "low", "close"])
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    df = df.sort_values("open_time").reset_index(drop=True)
    return (df["high"].to_numpy(float), df["low"].to_numpy(float),
            df["close"].to_numpy(float))


def _win(idx, n, high, low, close, atr, adx, lookback):
    """向量化：idx 上每个 i 的未来长度 n 窗口度量（含前半程/后半程分解）。"""
    c0, c1 = close[idx], close[idx + n]
    mid = idx + n // 2
    cm = close[mid]
    delta = c1 - c0
    eps = 1e-12
    atr_t = atr[idx]
    path = np.array([np.abs(np.diff(close[i:i + n + 1])).sum() for i in idx])
    p1 = np.array([np.abs(np.diff(close[i:m + 1])).sum() for i, m in zip(idx, mid)])
    p2 = np.array([np.abs(np.diff(close[m:i + n + 1])).sum() for i, m in zip(idx, mid)])
    prior_hi = np.array([high[i - lookback + 1:i + 1].max() for i in idx])
    prior_lo = np.array([low[i - lookback + 1:i + 1].min() for i in idx])
    fut_hi = np.array([high[i + 1:i + n + 1].max() for i in idx])
    fut_lo = np.array([low[i + 1:i + n + 1].min() for i in idx])
    wmax = np.array([close[i + 1:i + n + 1].max() for i in idx])
    wmin = np.array([close[i + 1:i + n + 1].min() for i in idx])
    return dict(
        er=np.abs(delta) / np.maximum(path, eps),
        disp=np.abs(delta) / atr_t,
        er1=np.abs(cm - c0) / np.maximum(p1, eps),
        disp1=np.abs(cm - c0) / atr_t,
        er2=np.abs(c1 - cm) / np.maximum(p2, eps),
        disp2=np.abs(c1 - cm) / atr_t,
        mae=np.where(delta >= 0, (c0 - wmin) / atr_t, (wmax - c0) / atr_t),
        adx_slope=(adx[idx + n] - adx[idx]) if adx is not None else None,
        new_ext=np.where(delta > 0, fut_hi > prior_hi,
                         np.where(delta < 0, fut_lo < prior_lo, False)),
    )


def metrics(high, low, close, n_main, n_fade, box_w):
    """主窗口全套 + 衰竭窗口 `_f` 字段（n_fade == n_main 时复用，零额外开销）。"""
    ind = SF.compute_indicators(high, low, close)
    atr, adx = ind["atr"], ind["adx"]
    win = 50
    i0 = max(SF.min_bars(), win + box_w) - 1
    i1 = len(close) - max(n_main, n_fade) - 1
    idx = np.arange(i0, i1 + 1)
    m = _win(idx, n_main, high, low, close, atr, adx, win)
    m["adx_t"] = adx[idx]
    fd = m if n_fade == n_main else _win(idx, n_fade, high, low, close, atr, adx, win)
    for k in ("er2", "mae", "adx_slope", "new_ext"):
        m[k + "_f"] = fd[k]
    m["idx"] = idx
    m["n"] = len(idx)
    return m


def classify(m, p):
    """向量化镜像（须与 B.classify_label 同口径，由 _consistency_check 守）。"""
    lab = np.full(m["n"], -1, dtype=int)
    osc = (m["er"] <= p["er_osc"]) & (m["disp"] <= p["disp_osc"])
    fade = ((m["adx_t"] >= p["adx_trend"]) & (m["adx_slope_f"] <= -p["adx_slope_min"])
            & ((m["er2_f"] <= p["er_fade"]) | (m["mae_f"] >= p["fade_ret_atr"]))
            & (~m["new_ext_f"]))
    init = ((m["disp1"] <= p["disp_init"]) & (m["er1"] <= p["er_osc"])
            & (m["disp2"] >= p["disp_min"]) & (m["er2"] >= p["er_trend"]))
    mid = (m["er"] >= p["er_trend"]) & (m["disp"] >= p["disp_min"])
    lab[mid] = 2
    lab[init] = 1
    lab[fade] = 3
    lab[osc] = 0
    return lab


def _full_keys(p: dict) -> dict:
    return {f"state.label.{k}": v for k, v in p.items()}


def _consistency_check(m, p, high, low, close, atr, adx, n_main, n_fade, lookback):
    rng = np.random.default_rng(7)
    picks = rng.choice(m["n"], size=min(200, m["n"]), replace=False)
    lab = classify(m, p)
    cfg = _full_keys(p)
    for j in picks:
        i = int(m["idx"][j])
        mm = B.label_metrics(i, n_main, n_fade, high, low, close, atr, adx, lookback)
        if mm is None:
            continue
        ref_name, _ = B.classify_label(mm, cfg)
        ref = B.STATE_ID[ref_name] if ref_name else -1
        if ref != int(lab[j]):
            raise SystemExit(
                f"[fatal] 标定镜像与生产口径不一致 @bar {i}: "
                f"mirror={lab[j]} ref={ref} — 请修正 calib_state_labels.classify")
    print("[check] 镜像与生产口径一致（200 抽样）", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="M5")
    ap.add_argument("--n-main", type=int, default=12)
    ap.add_argument("--n-fade", type=int, default=12)
    ap.add_argument("--box-window", type=int, default=20)
    ap.add_argument("--grid", choices=sorted(GRIDS), default="narrow",
                    help="网格预设；撞边界改用 wide")
    ap.add_argument("--max-amb", type=float, default=0.40,
                    help="允许的最大 ambiguous 占比（超过的组合直接丢弃）")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--json-out", default="", help="把 top-N 结果落盘（供变更说明引用）")
    args = ap.parse_args()

    high, low, close = load(args.symbol, args.tf)
    m = metrics(high, low, close, args.n_main, args.n_fade, args.box_window)
    print(f"[bars] {len(close)}  usable={m['n']}  "
          f"N_main={args.n_main} N_fade={args.n_fade}  grid={args.grid}", file=sys.stderr)

    grid = GRIDS[args.grid]
    keys = list(grid)
    n_combos = int(np.prod([len(grid[k]) for k in keys]))
    print(f"[grid] {n_combos} 组合", file=sys.stderr)

    rows = []
    for combo in product(*(grid[k] for k in keys)):
        p = dict(BASE, **dict(zip(keys, combo)))
        lab = classify(m, p)
        sh = np.array([(lab == c).sum() for c in range(4)]) / m["n"]
        amb = float((lab == -1).sum()) / m["n"]
        if amb > args.max_amb:
            continue
        rows.append((float(sh.min()), sh, p, amb))
    rows.sort(key=lambda r: -r[0])

    print(f"\n{'er_osc':>7}{'d_osc':>6}{'er_tr':>6}{'d_min':>6}{'er_fd':>6}"
          f"{'d_ini':>6}{'fd_r':>6}{'s_min':>6} |"
          f"{'osc%':>7}{'init%':>7}{'mid%':>7}{'fade%':>7}{'amb%':>7}")
    for _s, sh, p, amb in rows[:args.top]:
        print(f"{p['er_osc']:>7.2f}{p['disp_osc']:>6.2f}{p['er_trend']:>6.2f}"
              f"{p['disp_min']:>6.2f}{p['er_fade']:>6.2f}{p['disp_init']:>6.2f}"
              f"{p['fade_ret_atr']:>6.2f}{p['adx_slope_min']:>6.1f} |"
              f"{sh[0]*100:>7.1f}{sh[1]*100:>7.1f}{sh[2]*100:>7.1f}{sh[3]*100:>7.1f}"
              f"{amb*100:>7.1f}")
    if not rows:
        print(f"(无组合满足 ambiguous<={args.max_amb:.0%})")
        return

    # 撞边界检测（**关键**：命中即说明搜索空间被截断，结论不可用）
    best = rows[0][2]
    hits = [k for k in keys
            if best[k] in (min(grid[k]), max(grid[k])) and len(grid[k]) > 1]
    print(f"\n[best] {json.dumps({k: best[k] for k in keys}, ensure_ascii=False)}")
    print(f"[boundary] 撞边界参数 = {hits if hits else '无'}"
          + ("  ← 搜索空间可能被截断，建议换 --grid wide 重跑" if hits else ""))

    if args.json_out:
        out = {
            "symbol": args.symbol, "tf": args.tf, "grid": args.grid,
            "n_main": args.n_main, "n_fade": args.n_fade,
            "usable_bars": int(m["n"]), "max_amb": args.max_amb,
            "boundary_hits": hits,
            "top": [{
                "min_class_share": s,
                "shares": {NAMES[i]: float(sh[i]) for i in range(4)},
                "ambiguous": amb,
                "thresholds": p,
            } for s, sh, p, amb in rows[:args.top]],
        }
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"[out] {args.json_out}")

    ind = SF.compute_indicators(high, low, close)
    _consistency_check(m, rows[0][2], high, low, close, ind["atr"], ind["adx"],
                       args.n_main, args.n_fade, 50)


if __name__ == "__main__":
    main()
