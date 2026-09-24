#!/usr/bin/env python3
"""eval_vol_route_gate.py — 【P2 准入判据】vol 头能否做"波动路由"？（只读，离线）

**为什么要这道门**：4 类头的教训（§8.2-D3）—— 置信度"看起来可用"不等于**有经济含义**：
那时 `margin` 分桶与经济指标**反向**，直接接闸门会亏钱。故 vol 头在接线前必须过同一道门。

设计：读 `train_onset_model.py --oof-out` 落盘的 OOF 预测，按**校准后概率**分桶，
看**未来 12 bar 的实际波动幅度**是否随之单调 —— 并与**平凡基线**（当期 `atr_pct` 波动分位）
同口径对照。**打不过平凡基线 ⇒ 加模型无意义**（这正是 §9.2 对"镜像幅度"的判法）。

路由语义（要验证的命题）：
  · **低分位桶 ⇒ 未来振幅真的低** ⇒ 箱体/均值回归友好；
  · **高分位桶 ⇒ 未来振幅真的高** ⇒ 趋势单友好；
  · 且**桶间极差 ≥ 平凡规则的极差**（否则直接用 atr_pct 即可，不需要模型）。

自检（关键）：脚本会用 `close` 复算 `build_state_labels` 的 `vol_expansion` 定义，
与 OOF 里的 `y` 对账 —— **不一致率必须 ≈0**，否则说明两处口径已漂移（先修再谈结论）。

纪律：只读 PG/CSV，零生产影响。
用法：
  python tools/eval_vol_route_gate.py --oof tools/_scratch/vol_v90_oof.csv
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
BSL = _load("build_state_labels", os.path.join(_TOOLS, "build_state_labels.py"))

N_BINS = 5


def _table(tag: str, df: pd.DataFrame, key: str, col: str) -> dict:
    """按 `key` 分 5 桶，报告该桶的 `col` 均值 + 实际正例率 + 定向边际。"""
    q = pd.qcut(df[key], N_BINS, labels=False, duplicates="drop")
    print(f"\n  --- {tag} ---")
    print(f"  {'桶(低→高)':<10}{'n':>7}{'桶内均值':>10}{'实际扩张率':>11}"
          f"{'未来振幅(close/ATR)':>20}{'|净位移|/ATR':>14}{'定向边际/ATR':>14}")
    means = []
    for b in sorted(q.dropna().unique()):
        s = df[q == b]
        if len(s) < 50:
            continue
        m = float(s["amp_close_atr"].mean())
        means.append(m)
        print(f"  {int(b):<10}{len(s):>7}{s[key].mean():>10.3f}{s['y'].mean():>11.1%}"
              f"{m:>20.3f}{s['abs_net_atr'].mean():>14.3f}"
              f"{s['cont_atr'].mean():>14.3f}")
    return {"means": means,
            "spread": (max(means) - min(means)) if len(means) >= 2 else float("nan"),
            "ratio": (max(means) / min(means)) if len(means) >= 2 and min(means) > 0
            else float("nan")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--oof", default="tools/_scratch/vol_v90_oof.csv")
    ap.add_argument("--labels", default="tools/state_M5.csv",
                    help="提供当期 atr_pct（平凡基线用）")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="M5")
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", BSL.DB_URL_DEFAULT))
    ap.add_argument("--horizon", type=int, default=12)
    ap.add_argument("--vol-amp-min", type=float, default=3.13, dest="vol_amp_min")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    n = args.horizon
    o = pd.read_csv(args.oof)
    o["open_time"] = pd.to_datetime(o["open_time"], utc=True)
    if "oof_cal" not in o.columns:
        raise SystemExit("[fatal] OOF 缺 `oof_cal` 列（需 train_onset_model 新版产出）")

    conn = psycopg2.connect(args.db_url)
    try:
        kl = BSL.load_klines(conn, args.symbol, args.tf)
    finally:
        conn.close()
    high = kl["high"].to_numpy(dtype=float)
    low = kl["low"].to_numpy(dtype=float)
    close = kl["close"].to_numpy(dtype=float)
    atr = SF.compute_indicators(high, low, close, dict(SF.DEFAULT_PARAMS))["atr"]

    idx_by_time = {t: i for i, t in enumerate(kl["open_time"])}
    i_arr = o["open_time"].map(idx_by_time).to_numpy()
    bad = np.isnan(i_arr)
    if bad.any():
        print(f"[warn] {int(bad.sum())} 行 open_time 在 klines 中找不到 → 丢弃", file=sys.stderr)
    i_arr = i_arr[~bad].astype(int)
    o = o[~bad].reset_index(drop=True)

    # ── 未来度量（与 build_state_labels 的 vol_expansion **同口径**：基于 close）──
    cl = pd.Series(close)
    win = cl.rolling(n).max().shift(-n) - cl.rolling(n).min().shift(-n)
    amp_close_atr = (win / pd.Series(atr)).to_numpy()[i_arr]
    c0, cN = close[i_arr], close[i_arr + n]
    with np.errstate(invalid="ignore", divide="ignore"):
        abs_net_atr = np.abs(cN - c0) / atr[i_arr]
        past_net = c0 - close[i_arr - n]
        cont_atr = (cN - c0) * np.sign(past_net) / atr[i_arr]
        amp_hl_atr = (pd.Series(high).rolling(n).max().shift(-n)
                      - pd.Series(low).rolling(n).min().shift(-n)).to_numpy()[i_arr] / atr[i_arr]

    df = pd.DataFrame({
        "oof": o["oof"].to_numpy(), "oof_cal": o["oof_cal"].to_numpy(),
        "y": o["y"].to_numpy(), "amp_close_atr": amp_close_atr,
        "abs_net_atr": abs_net_atr, "cont_atr": cont_atr, "amp_hl_atr": amp_hl_atr,
    }).replace([np.inf, -np.inf], np.nan).dropna()

    # ── 自检：复算的 vol_expansion 必须与 OOF 的 y 一致（口径漂移检测）──
    recomputed = (df["amp_close_atr"] >= args.vol_amp_min).astype(int)
    mismatch = float((recomputed != df["y"]).mean())
    print(f"[自检] vol_expansion 复算 vs OOF y 不一致率 = {mismatch:.4%}"
          f"（{'✓ 口径一致' if mismatch < 0.01 else '✗ 口径已漂移，结论不可信'}）")
    print(f"[data] 样本 {len(df)}  horizon={n}bar  正例率={df['y'].mean():.1%}")

    # 平凡基线：当期波动分位（唯一需要的过去量）
    lab = pd.read_csv(args.labels)
    lab["open_time"] = pd.to_datetime(lab["open_time"], utc=True)
    apct = lab.set_index("open_time")["atr_pct"]
    # 按 open_time 取当期波动分位（df.index 是 o 的子集，位置对齐由 pandas 保证）
    df["atr_pct"] = o.loc[df.index, "open_time"].map(apct).to_numpy()
    df = df.replace([np.inf, -np.inf], np.nan).dropna(subset=["atr_pct"])
    print(f"[基线] atr_pct 覆盖 {len(df)} 行（当期波动分位，0~100）")

    print("\n=========== 经济分层对照（同口径）===========")
    print("  [读法] 「实际扩张率」应随桶上升而上升（校准有效）；")
    print("         「未来振幅(close/ATR)」的**桶间极差** = 路由可用性的直接度量。")
    mod = _table("模型：按校准后概率 oof_cal 分桶", df, "oof_cal", "oof_cal")
    base = _table("平凡基线：按当期 atr_pct 分桶", df, "atr_pct", "atr_pct")

    print("\n=========== 判据 ===========")
    print(f"  · 模型分桶「未来振幅」极差 = {mod['spread']:.3f} ATR"
          f"（最高桶/最低桶 = {mod['ratio']:.2f}×）")
    print(f"  · 基线分桶「未来振幅」极差 = {base['spread']:.3f} ATR"
          f"（最高桶/最低桶 = {base['ratio']:.2f}×）")
    print(f"  · 模型 vs 基线极差 = {mod['spread'] - base['spread']:+.3f} ATR")
    ok = (mod["spread"] >= base["spread"]) and np.isfinite(mod["spread"])
    print("\n  " + ("**通过**：模型的波动分层不劣于平凡基线 ⇒ 可用于路由"
                    "（低分位→箱体 / 高分位→趋势）。"
                    if ok else
                    "**不通过**：模型分层不优于「当期 atr_pct」⇒ **不该为路由加模型**，"
                    "直接用 atr_pct 分位即可（更简单、无模型风险）。"))


if __name__ == "__main__":
    main()
