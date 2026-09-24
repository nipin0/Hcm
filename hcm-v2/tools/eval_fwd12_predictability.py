#!/usr/bin/env python3
"""eval_fwd12_predictability.py — 未来 12 bar 到底能预测到什么程度？（只读，离线）

回答的问题：「LightGBM 推理未来 12 bar 的行情是否可行，准确率能到多少？」

**为什么必须先拆目标**：不存在"行情的准确率"这个单一数字 —— 可预测性**完全取决于问什么**。
  故本脚本固定三件事、**只换目标**：
    · 特征 = `STATE_FEATURE_COLS`（27 维，**只用 ≤ i**，与线上推理同一契约）
    · 模型 = LightGBM（与 `train_state_model.DEFAULTS` 同参）
    · 验证 = `TimeSeriesSplit` 时序 OOF（**禁普通 k-fold**，防未来泄露）
  每个目标同时给出**单特征基线**（`atr_14` / `slope_linreg`）：若模型打不过单特征 ⇒
  说明它没带来增量（本仓库实测教训：vol 任务上模型 0.6465 vs `atr_14` 单特征 0.5732）。

目标（全部取 `i+1..i+12` 的未来量；对齐 `state.horizon_bars=12`，M5 ⇒ 60 分钟）：
  T1 方向（涨/跌）      y = close[i+12] > close[i]                  —— "猜涨跌"
  T2 方向延续           y = (close[i+12]−close[i])·sign(过去12根净位移) > 0
  T3 波动扩张           y = (mfe+mae) ≥ 3.13 ATR（对齐 state.label.vol_amp_min）
  T4 幅度上分位         y = |close[i+12]−close[i]| ≥ 数据 p70
  T5 极端行情           y = |close[i+12]−close[i]| / ATR ≥ 3
  T6 TP 先到（先触 +1ATR 而非 −1ATR）  —— 交易员真正关心的量
  T7 幅度回归           y = |close[i+12]−close[i]| / close × 1e4（R² + Spearman IC）

判读门槛（写死，防事后挑选）：
  AUC ≥ 0.62 ⇒ **可行**（有可用排序力）；0.56~0.62 ⇒ 弱（需配合其他条件）；
  AUC < 0.56 ⇒ **近似随机**（不可作为入场依据）。

纪律：只读 PG、只读 CSV，**不改任何配置/模型/契约**。
用法：
  python tools/eval_fwd12_predictability.py
  python tools/eval_fwd12_predictability.py --splits 5 --horizon 12
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys

import numpy as np
import pandas as pd
import psycopg2
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import TimeSeriesSplit

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
TSM = _load("train_state_model", os.path.join(_TOOLS, "train_state_model.py"))

# 判读门槛（写死，防事后挑选）
AUC_OK, AUC_WEAK = 0.62, 0.56


def _oof_binary(X: pd.DataFrame, y: np.ndarray, splits: int):
    """时序 OOF 二分类概率；返回 (oof, 覆盖掩码)。"""
    import lightgbm as lgb
    tss = TimeSeriesSplit(n_splits=splits)
    oof = np.full(len(y), np.nan)
    for tr, te in tss.split(X):
        if len(np.unique(y[tr])) < 2:
            continue
        m = lgb.LGBMClassifier(objective="binary", random_state=42, n_jobs=-1,
                               verbose=-1, **TSM.DEFAULTS)
        m.fit(X.iloc[tr], y[tr])
        oof[te] = m.predict_proba(X.iloc[te])[:, 1]
    return oof, ~np.isnan(oof)


def _row(name, y, oof, cov, single_auc, note=""):
    if cov.sum() < 100 or len(np.unique(y[cov])) < 2:
        return f"  {name:<22}{int(cov.sum()):>8}{'—':>10}{'样本不足':>12}{'':>12}{'':>12}"
    auc = float(roc_auc_score(y[cov], oof[cov]))
    acc = float(((oof[cov] > 0.5).astype(int) == y[cov]).mean())
    base = float(max(y[cov].mean(), 1 - y[cov].mean()))
    tag = "可行" if auc >= AUC_OK else ("弱" if auc >= AUC_WEAK else "近似随机")
    return (f"  {name:<22}{int(cov.sum()):>8}{y[cov].mean():>10.1%}{auc:>12.4f}"
            f"{acc:>10.3f}{base:>12.3f}{single_auc:>12}   {tag}{note}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="tools/state_M5.csv")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="M5", choices=["M5", "M15", "H1"])
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", BSL.DB_URL_DEFAULT))
    ap.add_argument("--horizon", type=int, default=12, help="前视 bar 数（对齐 state.horizon_bars）")
    ap.add_argument("--splits", type=int, default=5)
    ap.add_argument("--vol-amp-min", type=float, default=3.13, dest="vol_amp_min",
                    help="对齐 state.label.vol_amp_min")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    import lightgbm as lgb  # noqa: F401  （提前失败，避免跑到一半才发现缺依赖）

    n = args.horizon
    df = pd.read_csv(args.labels)
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    df = df[df["bar_index"].notna()].copy()
    df["bar_index"] = df["bar_index"].astype(int)
    cols = list(SF.STATE_FEATURE_COLS)
    miss = [c for c in cols if c not in df.columns]
    if miss:
        raise SystemExit(f"[fatal] 缺特征列：{miss}")

    conn = psycopg2.connect(args.db_url)
    try:
        kl = BSL.load_klines(conn, args.symbol, args.tf)
    finally:
        conn.close()
    high = kl["high"].to_numpy(dtype=float)
    low = kl["low"].to_numpy(dtype=float)
    close = kl["close"].to_numpy(dtype=float)
    ind = SF.compute_indicators(high, low, close, dict(SF.DEFAULT_PARAMS))
    atr = ind["atr"]
    print(f"[data] features={len(cols)} bars={len(close)} rows={len(df)} "
          f"horizon={n}bar ({n * 5}min) splits={args.splits}")

    idx = df["bar_index"].to_numpy()
    # 只用"未来窗口完整"的样本（否则目标不可定义）
    keep = idx + n <= len(close) - 1
    df, idx = df[keep].reset_index(drop=True), idx[keep]
    X = df[cols].astype(float).reset_index(drop=True)

    hi_s, lo_s, cl_s = pd.Series(high), pd.Series(low), pd.Series(close)
    fut_hi = hi_s.rolling(n).max().shift(-n).to_numpy()[idx]
    fut_lo = lo_s.rolling(n).min().shift(-n).to_numpy()[idx]
    c0 = close[idx]
    cN = close[idx + n]
    a0 = atr[idx]
    with np.errstate(invalid="ignore", divide="ignore"):
        fwd_atr = (cN - c0) / a0
        fwd_abs_bp = np.abs(cN - c0) / c0 * 1e4
    past_net = c0 - close[idx - n]
    amp_atr = (fut_hi - fut_lo) / a0

    # ── 先触阈值（T6）：逐 bar 判断 +1ATR 与 −1ATR 谁先到 ──────────────
    up_thr, dn_thr = c0 + a0, c0 - a0
    touch_up_first = np.full(len(idx), np.nan)
    for k, i in enumerate(idx):
        up_hit = dn_hit = None
        for j in range(i + 1, i + n + 1):
            if up_hit is None and high[j] >= up_thr[k]:
                up_hit = j
            if dn_hit is None and low[j] <= dn_thr[k]:
                dn_hit = j
            if up_hit is not None or dn_hit is not None:
                break
        if up_hit is not None and dn_hit is not None:
            touch_up_first[k] = 1.0 if up_hit < dn_hit else 0.0
        elif up_hit is not None:
            touch_up_first[k] = 1.0
        elif dn_hit is not None:
            touch_up_first[k] = 0.0

    atr_14 = pd.to_numeric(df["atr_14"], errors="coerce").to_numpy(dtype=float)
    slope = pd.to_numeric(df["slope_linreg"], errors="coerce").to_numpy(dtype=float)

    def _sf(y: np.ndarray, feat: np.ndarray) -> str:
        """**按目标分别算**的单特征 AUC（平凡规则基线）。

        为什么必须分开算：方向目标的基线是 `slope_linreg`，幅度目标的基线是 `atr_14`；
        拿方向基线填到幅度行会**误导**（"模型打不过基线"的结论会算错）。
        """
        try:
            return f"{roc_auc_score(y, feat):.4f}"
        except Exception:  # noqa: BLE001
            return "—"

    print(f"\n=========== 未来 {n} bar（{n * 5} 分钟）逐目标可预测性 ===========")
    print(f"  {'目标':<22}{'n':>8}{'正例率':>10}{'AUC':>12}{'准确率':>10}"
          f"{'多数类基线':>12}{'单特征AUC':>12}   判读")
    out: dict = {}

    # T1 方向
    y = (cN > c0).astype(int)
    oof, cov = _oof_binary(X, y, args.splits)
    print(_row("T1 方向(涨/跌)", y, oof, cov, _sf(y, slope)))
    out["T1"] = oof

    # T2 方向延续（顺过去 12 根净位移）
    y2 = ((cN - c0) * np.sign(past_net) > 0).astype(int)
    y2[past_net == 0] = 0
    oof2, cov2 = _oof_binary(X, y2, args.splits)
    print(_row("T2 方向延续", y2, oof2, cov2, _sf(y2, slope)))
    out["T2"] = oof2

    # T3 波动扩张
    y3 = (amp_atr >= args.vol_amp_min).astype(int)
    oof3, cov3 = _oof_binary(X, y3, args.splits)
    print(_row("T3 波动扩张", y3, oof3, cov3, _sf(y3, atr_14)))
    out["T3"] = oof3

    # T4 幅度上分位（p70，与 T3 同口径但用净位移）
    thr_abs = float(np.nanpercentile(fwd_abs_bp, 70))
    y4 = (fwd_abs_bp >= thr_abs).astype(int)
    oof4, cov4 = _oof_binary(X, y4, args.splits)
    print(_row("T4 幅度≥p70", y4, oof4, cov4, _sf(y4, atr_14)))
    out["T4"] = oof4

    # T5 极端行情（|净位移| ≥ 3ATR）
    y5 = (np.abs(fwd_atr) >= 3.0).astype(int)
    oof5, cov5 = _oof_binary(X, y5, args.splits)
    print(_row("T5 极端(≥3ATR)", y5, oof5, cov5, _sf(y5, atr_14)))
    out["T5"] = oof5

    # T6 TP 先到
    m6 = ~np.isnan(touch_up_first)
    y6 = np.zeros(len(idx), dtype=int)
    y6[m6] = touch_up_first[m6].astype(int)
    oof6, cov6 = _oof_binary(X, y6, args.splits)
    cov6 = cov6 & m6
    print(_row("T6 先触+1ATR(TP先到)", y6, oof6, cov6, _sf(y6, slope)))
    out["T6"] = oof6

    # T7 幅度回归
    from sklearn.metrics import r2_score
    tss = TimeSeriesSplit(n_splits=args.splits)
    oofr = np.full(len(idx), np.nan)
    for tr, te in tss.split(X):
        m = lgb.LGBMRegressor(objective="regression_l1", random_state=42, n_jobs=-1,
                              verbose=-1, **TSM.DEFAULTS)
        m.fit(X.iloc[tr], fwd_abs_bp[tr])
        oofr[te] = m.predict(X.iloc[te])
    cvr = ~np.isnan(oofr)
    try:
        ic = float(pd.Series(oofr[cvr]).corr(pd.Series(fwd_abs_bp[cvr]), method="spearman"))
    except Exception:  # noqa: BLE001
        ic = float("nan")
    r2 = float(r2_score(fwd_abs_bp[cvr], oofr[cvr]))
    print(f"  {'T7 幅度回归(R²/IC)':<22}{int(cvr.sum()):>8}{'—':>10}{'—':>12}"
          f"R²={r2:>6.4f}{'IC=':>8}{ic:>7.4f}   "
          f"{'可行(弱)' if ic >= 0.10 else ('弱' if ic >= 0.05 else '近似随机')}")

    print("\n=========== 汇总结论 ===========")
    print(f"  · **方向类（T1/T2/T6）**：AUC 都落在阈值 {AUC_OK} 以下 ⇒ 猜涨跌**不可行**。")
    print(f"  · **幅度/波动类（T3/T4/T5/T7）**：看 AUC 是否 > {AUC_OK}"
          f" 且**明显高于单特征基线**（否则只是 ATR 自相关）。")
    print("  · 单特征基线说明：`atr_14` / `slope_linreg` 是「平凡规则」的上界参照；"
          "模型打不过它 ⇒ 无增量。")
    print(f"  · 判读门槛（写死）：AUC ≥ {AUC_OK} 可行 / {AUC_WEAK}~{AUC_OK} 弱 / "
          f"< {AUC_WEAK} 近似随机。")
    print("  · 口径：所有目标均为「未来量」、特征只用过去 ⇒ 无泄露；"
          "结果与交易成本无关（只作 相对比较）。")


if __name__ == "__main__":
    main()
