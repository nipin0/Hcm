#!/usr/bin/env python3
"""build_state_memory_features.py — 【D2】「镜像记忆」候选特征构造器（只读，离线）。

背景（2026-09-19，两项已定案的诊断）：
  · D1（`eval_state_separability.py --feature-set oracle`）：把 `classify_label` 判类所用的
    **未来窗口度量**当特征，6 个两两组合 **AUC 全部 = 1.0000**、条件分布总变差 **99.6~99.9%**
    ⇒ **4 类标签是未来窗口的确定性函数**；而 base(27 维过去特征) 只有 0.52~0.80。
  · §7.6（补量价/点差 6 列）、§7.7（按分位重定阈值）**两条路都已实测失败**
    ⇒ 剩下的最后一个未验证假设就是本脚本：

  **把标签用到的每一个未来度量，在"过去窗口"上镜像算一遍，能否让
  「oscillation / trend_init / trend_mid」变得可分？**

判据（决定下一步走哪条路，**不预设结论**）：
  · 关键对 AUC 明显上升（> 0.62）⇒ 判别信息确实存在，只是特征缺了"状态记忆"
    ⇒ 应把这些列升级进 `state_features.py` 契约并重训（走"与重训同批原子切换"流程）；
  · 若纹丝不动（仍 0.51~0.56）⇒ **过去的镜像量不携带未来相位的信息**
    ⇒ 补特征路线关闭，必须改**目标**（方案 §7.8 的 ③-A）。

纪律：
  * 只读 PG、只写 CSV，**不改任何生产配置/模型/契约**。
  * 镜像列**刻意不进** `state_features.STATE_FEATURE_COLS`：该文件是**推理共享契约**
    （`check_feature_contract` 严格相等校验，改动会立刻拒载线上模型），
    按 L1 段既定纪律，契约变更必须与"重训 + 换模型"**原子切换**。
    故本脚本是"镜像候选集"的**唯一实现处**；若 D2 通过，再整体移入契约。
  * 无未来泄露：全部列只用 `≤ i` 的已收盘 bar。

用法：
  python build_state_memory_features.py --labels tools/state_M5.csv \
      --out tools/_scratch/state_M5_mem.csv
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys

import numpy as np
import pandas as pd
import psycopg2

_TOOLS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_TOOLS)
_SIG = os.path.join(_ROOT, "hcm-signal-tower", "signal_tower")


def _load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


SF = _load("state_features", os.path.join(_SIG, "state_features.py"))
# 复用 K 线加载 / 连接串默认值（**唯一实现处**在 build_state_labels，避免两份 SQL 漂移）
BSL = _load("build_state_labels", os.path.join(_TOOLS, "build_state_labels.py"))

# ── 镜像候选列（顺序即契约；键名与 `compute_memory` 的返回键**必须逐字一致**）──────
# 命名规则：`<标签侧度量名>_trail<n>` = 该未来度量在"过去 n 根"上的镜像；
#   `_lag<k>` = 同一镜像量在 k 根之前的取值（承载"趋势刚起 vs 已持续"的**轨迹**）。
MEM_FEATURE_COLS = [
    "er_trail12",          # 镜像标签 `er`（过去 12 根效率比）
    "disp_trail12",        # 镜像标签 `disp`（过去 12 根净位移 / ATR）
    "er1_trail12",         # 镜像标签 `er1`（过去窗口**前半**效率比）
    "er2_trail12",         # 镜像标签 `er2`（过去窗口**后半**效率比）
    "disp1_trail12",       # 镜像标签 `disp1`（前半净位移 / ATR）
    "disp2_trail12",       # 镜像标签 `disp2`（后半净位移 / ATR）
    "adx_slope_trail12",   # 镜像标签 `adx_slope`（ADX 在过去 12 根的变化）
    "mae_trail12",         # 镜像标签 `mae_atr`（过去 12 根最大逆行 / ATR）
    "mfe_trail12",         # 镜像标签 `mfe_atr`（过去 12 根最大顺行 / ATR）
    "er_trail12_lag6",     # 轨迹：6 根之前的 er_trail12
    "er_trail12_lag12",    # 轨迹：12 根之前的 er_trail12
    "bars_since_hi_50",    # 距过去 50 根最高点的 bar 数 / 50（"状态记忆"，标签无对应量）
    "bars_since_lo_50",    # 距过去 50 根最低点的 bar 数 / 50（同上）
]


def _path(a: np.ndarray) -> float:
    """路径长度（相邻收盘价绝对差之和）；<2 点返回 0。"""
    if len(a) < 2:
        return 0.0
    return float(np.abs(np.diff(a)).sum())


def _trail_er(close: np.ndarray, i: int, n: int) -> float:
    """i 往前 n 根的效率比（镜像 `window_metrics` 的 er，方向相反）。"""
    if i - n < 0:
        return float("nan")
    p = _path(close[i - n: i + 1])
    return abs(float(close[i]) - float(close[i - n])) / p if p > 1e-12 else 0.0


def compute_memory(i: int, high: np.ndarray, low: np.ndarray, close: np.ndarray,
                   atr: np.ndarray, adx: np.ndarray, n: int = 12,
                   lookback: int = 50) -> dict | None:
    """bar i 的镜像记忆特征（**只用 ≤ i**）。窗口不足/ATR 不可用 → None。

    口径与 `build_state_labels.window_metrics` **逐项镜像**：那边算 `close[i .. i+n]`，
    这边算 `close[i-n .. i]`；`mae/mfe` 的"顺/逆"以**过去**净位移方向为参考。
    """
    if i - n < 0 or i - lookback + 1 < 0:
        return None
    atr_t = float(atr[i])
    if not np.isfinite(atr_t) or atr_t <= 0.0:
        return None

    c0 = float(close[i - n])
    c1 = float(close[i])
    net = c1 - c0
    path = _path(close[i - n: i + 1])
    mid = i - n // 2
    c_mid = float(close[mid])
    path1 = _path(close[i - n: mid + 1])
    path2 = _path(close[mid: i + 1])

    hi_win = high[i - lookback + 1: i + 1]
    lo_win = low[i - lookback + 1: i + 1]
    # 距极值的 bar 数：切片下标 j ↔ bar (i-lookback+1+j) ⇒ 距离 = lookback-1-j
    since_hi = (lookback - 1 - int(np.argmax(hi_win))) / float(lookback)
    since_lo = (lookback - 1 - int(np.argmin(lo_win))) / float(lookback)

    out = {
        "er_trail12": abs(net) / path if path > 1e-12 else 0.0,
        "disp_trail12": abs(net) / atr_t,
        "er1_trail12": abs(c_mid - c0) / path1 if path1 > 1e-12 else 0.0,
        "er2_trail12": abs(c1 - c_mid) / path2 if path2 > 1e-12 else 0.0,
        "disp1_trail12": abs(c_mid - c0) / atr_t,
        "disp2_trail12": abs(c1 - c_mid) / atr_t,
        "adx_slope_trail12": float(adx[i]) - float(adx[i - n]),
        "bars_since_hi_50": since_hi,
        "bars_since_lo_50": since_lo,
        # 轨迹（滞后）：缺样本用 NaN —— LightGBM 原生处理缺失，**不猜数值**
        "er_trail12_lag6": _trail_er(close, i - 6, n),
        "er_trail12_lag12": _trail_er(close, i - 12, n),
    }
    w = close[i - n + 1: i + 1]
    if net >= 0:
        out["mae_trail12"] = float(c0 - w.min()) / atr_t
        out["mfe_trail12"] = float(w.max() - c0) / atr_t
    else:
        out["mae_trail12"] = float(w.max() - c0) / atr_t
        out["mfe_trail12"] = float(c0 - w.min()) / atr_t
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="tools/state_M5.csv",
                    help="build_state_labels.py 的产物（提供 open_time / label_id / base 特征）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="M5", choices=["M5", "M15", "H1"])
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", BSL.DB_URL_DEFAULT))
    ap.add_argument("--horizon", type=int, default=12,
                    help="镜像窗口长度 n（对齐 state.horizon_bars，默认 12）")
    ap.add_argument("--lookback", type=int, default=50,
                    help="极值回看窗口（对齐 state_features.win_window，默认 50）")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    lab = pd.read_csv(args.labels)
    lab["open_time"] = pd.to_datetime(lab["open_time"], utc=True)
    print(f"[labels] {args.labels} rows={len(lab)}", file=sys.stderr)

    conn = psycopg2.connect(args.db_url)
    try:
        kl = BSL.load_klines(conn, args.symbol, args.tf)
    finally:
        conn.close()
    if kl.empty:
        raise SystemExit(f"[fatal] no klines for {args.symbol} {args.tf}")
    print(f"[klines] {args.symbol} {args.tf} bars={len(kl)} "
          f"range={kl['open_time'].iloc[0]} .. {kl['open_time'].iloc[-1]}", file=sys.stderr)

    high = kl["high"].to_numpy(dtype=float)
    low = kl["low"].to_numpy(dtype=float)
    close = kl["close"].to_numpy(dtype=float)
    params = dict(SF.DEFAULT_PARAMS)
    ind = SF.compute_indicators(high, low, close, params)
    atr, adx = ind["atr"], ind["adx"]

    idx_by_time = {t: i for i, t in enumerate(kl["open_time"])}
    rows, miss_time, miss_calc = [], 0, 0
    nan_row = {c: float("nan") for c in MEM_FEATURE_COLS}
    for t in lab["open_time"]:
        i = idx_by_time.get(t)
        if i is None:
            miss_time += 1
            rows.append(dict(nan_row))
            continue
        m = compute_memory(i, high, low, close, atr, adx,
                           n=args.horizon, lookback=args.lookback)
        if m is None:
            miss_calc += 1
            rows.append(dict(nan_row))
        else:
            rows.append(m)

    # 列顺序**强制**对齐 MEM_FEATURE_COLS：`rows` 是 dict，插入顺序与常量声明顺序不同
    # （实测踩到：`mae/mfe` 在 dict 字面量之后才赋值 ⇒ 顺序漂移，被下方自检拦下）。
    mem = pd.DataFrame(rows, columns=MEM_FEATURE_COLS)
    out = pd.concat([lab, mem], axis=1)
    # 契约自检：列名/顺序必须与 MEM_FEATURE_COLS 完全一致（本脚本内唯一的强校验点）
    got = [c for c in out.columns if c in MEM_FEATURE_COLS]
    if got != MEM_FEATURE_COLS:
        raise SystemExit(f"[fatal] 列契约不一致：期望 {MEM_FEATURE_COLS}，实得 {got}")
    out.to_csv(args.out, index=False)

    nn = out[MEM_FEATURE_COLS].notna().all(axis=1).sum()
    print(f"[done] rows={len(out)} 镜像列齐全={nn} 未匹配时间={miss_time} "
          f"窗口不足={miss_calc}", file=sys.stderr)
    print(f"[out] {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
