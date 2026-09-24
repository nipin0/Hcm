#!/usr/bin/env python3
"""eval_direction_mtf.py — 方向类的**最后一个杠杆**：多周期上下文（只读，离线）

背景：§10 实测「未来 12 bar 方向」用 M5 单周期 27 维特征 AUC 仅 **0.5119**（≈随机）。
`eval_anticipation_bound.py` 的文档已声明：「上限是**相对于本特征集**的；多周期/量价/盘口
特征可抬高它」。本脚本就是把这句话测掉，从而**给方向问题封口**。

设计（**只换特征集，目标与验证口径完全不变**）：
  · 目标：未来 12 bar（60 分钟）
      T1 方向   y = close[i+12] > close[i]
      T2 延续   y = (close[i+12]−close[i]) · sign(过去 12 根净位移) > 0
      T6 TP先到  y = 未来 12 根先触 +1ATR（而非先触 −1ATR）
  · 臂（arms）：
      A0 base        = M5 27 维（= §10 基线，应复现 0.5119）
      A1 base+h1     = + H1 的 27 维（同一 `state_features` 契约）+ 5 个跨周期交互量
      A2 base+全周期 = + M30 + H1 + H4 + D1（4×27）+ 跨周期交互量
  · 模型 / 验证：LightGBM（同 `train_state_model.DEFAULTS`）+ `TimeSeriesSplit` 时序 OOF

**因果对齐（关键，防泄露）**：对每根 M5 bar（open_time = t），高周期只取
`close_time ≤ t` 的**最后一根已收盘** bar —— 用 `searchsorted(open+TF, t, 'right')−1`。
（同一根 M5 bar 只有一根对齐的高周期 bar ⇒ 未来信息不可能进来。）

判据（写死，防事后挑选）：
  · **有杠杆**：某臂 ΔAUC(vs A0) ≥ **+0.02** 且 AUC ≥ **0.56**；
  · 否则 **封口**：方向不可预测，与特征集无关。

纪律：只读 PG/CSV，**不改任何配置/模型/契约**。
用法：
  python tools/eval_direction_mtf.py
  python tools/eval_direction_mtf.py --splits 5 --horizon 12
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

# 判据门槛（写死，防事后挑选）
DELTA_LEVER, AUC_FLOOR = 0.02, 0.56
# 高周期 → 每根 bar 的分钟数（用于 close_time = open_time + N 分钟）
TF_MINUTES = {"M30": 30, "H1": 60, "H4": 240, "D1": 1440}


def tf_context(conn, symbol: str, tf: str, m5_epoch: np.ndarray, params: dict):
    """返回该周期的**因果对齐**特征矩阵（每根 M5 bar 对应其最后一根已收盘高周期 bar）。

    性能要点：同一根高周期 bar 会被很多 M5 bar 复用 ⇒ **按唯一下标算一次再映射**
    （否则 4 个周期 × 5.7 万行 = 22.8 万次特征计算，慢一个量级）。
    """
    kl = BSL.load_klines(conn, symbol, tf)
    if kl.empty:
        return None, None
    oe = BSL.epoch_s(kl["open_time"])
    ce = oe + TF_MINUTES[tf] * 60                     # 该 bar 的收盘时刻
    pos = np.searchsorted(ce, m5_epoch, side="right") - 1   # 最后一根"已收盘"的
    hi = kl["high"].to_numpy(dtype=float)
    lo = kl["low"].to_numpy(dtype=float)
    cl = kl["close"].to_numpy(dtype=float)
    ind = SF.compute_indicators(hi, lo, cl, params)
    uniq = np.unique(pos[pos >= 0])
    cache: dict = {}
    for i in uniq:
        f = SF.compute_features_at(int(i), hi, lo, cl, ind, oe, params)
        if f is not None:
            cache[int(i)] = {k: float(f[k]) for k in SF.STATE_FEATURE_COLS}
    cols = [f"{tf.lower()}_{k}" for k in SF.STATE_FEATURE_COLS]
    mat = np.full((len(pos), len(cols)), np.nan)
    for r, i in enumerate(pos):
        c = cache.get(int(i)) if i >= 0 else None
        if c:
            mat[r] = [c[k] for k in SF.STATE_FEATURE_COLS]
    print(f"  [{tf}] bars={len(kl)} 唯一对齐={len(uniq)} 覆盖={np.isfinite(mat[:, 0]).mean():.1%}",
          file=sys.stderr)
    return pd.DataFrame(mat, columns=cols), kl


def oof_auc(X: pd.DataFrame, y: np.ndarray, splits: int):
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
    cov = ~np.isnan(oof)
    if cov.sum() < 100 or len(np.unique(y[cov])) < 2:
        return float("nan"), float("nan"), float("nan"), int(cov.sum())
    return (float(roc_auc_score(y[cov], oof[cov])),
            float(((oof[cov] > 0.5).astype(int) == y[cov]).mean()),
            float(max(y[cov].mean(), 1 - y[cov].mean())), int(cov.sum()))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="tools/state_M5.csv")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--tf", default="M5")
    ap.add_argument("--db-url", default=os.environ.get("DB_URL", BSL.DB_URL_DEFAULT))
    ap.add_argument("--horizon", type=int, default=12)
    ap.add_argument("--splits", type=int, default=5)
    ap.add_argument("--arms", default="A0,A1,A2",
                    help="A0=base / A1=base+H1 / A2=base+全周期(M30,H1,H4,D1)")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    n = args.horizon
    df = pd.read_csv(args.labels)
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    df = df[df["bar_index"].notna()].copy()
    df["bar_index"] = df["bar_index"].astype(int)
    base_cols = list(SF.STATE_FEATURE_COLS)

    conn = psycopg2.connect(args.db_url)
    try:
        kl5 = BSL.load_klines(conn, args.symbol, args.tf)
        high = kl5["high"].to_numpy(dtype=float)
        low = kl5["low"].to_numpy(dtype=float)
        close = kl5["close"].to_numpy(dtype=float)
        atr = SF.compute_indicators(high, low, close, dict(SF.DEFAULT_PARAMS))["atr"]
        idx = df["bar_index"].to_numpy()
        keep = idx + n <= len(close) - 1
        df, idx = df[keep].reset_index(drop=True), idx[keep]
        m5_epoch = BSL.epoch_s(kl5["open_time"])[idx]
        params = dict(SF.DEFAULT_PARAMS)

        # ── 目标（未来量，特征绝不含）─────────────────────────────────
        c0, cN, a0 = close[idx], close[idx + n], atr[idx]
        y_dir = (cN > c0).astype(int)
        past_net = c0 - close[idx - n]
        y_cont = ((cN - c0) * np.sign(past_net) > 0).astype(int)
        hi_s, lo_s = pd.Series(high), pd.Series(low)
        # T6：先触 +1ATR 还是 −1ATR
        up_thr, dn_thr = c0 + a0, c0 - a0
        t6 = np.full(len(idx), np.nan)
        for k, i in enumerate(idx):
            for j in range(i + 1, i + n + 1):
                u, d = high[j] >= up_thr[k], low[j] <= dn_thr[k]
                if u or d:
                    t6[k] = 1.0 if (u and not d) else (0.0 if (d and not u) else np.nan)
                    break
        m6 = ~np.isnan(t6)
        y_tp = np.zeros(len(idx), dtype=int)
        y_tp[m6] = t6[m6].astype(int)
        print(f"[data] M5 样本={len(idx)} horizon={n}bar  "
              f"方向正例率={y_dir.mean():.1%} 延续={y_cont.mean():.1%} TP先到={y_tp[m6].mean():.1%}")

        # ── 多周期上下文（因果对齐）──────────────────────────────────
        # 只在真的用到时才算（`--arms A0,A3` 时跳过，省 ~2 分钟）
        ctx: dict = {}
        if any(a in args.arms for a in ("A1", "A2")):
            for tf in ("M30", "H1", "H4", "D1"):
                m, _ = tf_context(conn, args.symbol, tf, m5_epoch, params)
                if m is not None:
                    ctx[tf] = m
    finally:
        conn.close()

    X0 = df[base_cols].astype(float).reset_index(drop=True)
    slope5 = pd.to_numeric(df["slope_linreg"], errors="coerce").to_numpy(dtype=float)
    atr5 = pd.to_numeric(df["atr_14"], errors="coerce").to_numpy(dtype=float)

    def cross_feats(tags):
        """跨周期交互量：**符号一致性需要相乘，树模型构造不出**，故显式给出。"""
        out, names = [], []
        for t in tags:
            if t not in ctx:
                continue
            sl = pd.to_numeric(ctx[t][f"{t.lower()}_slope_linreg"], errors="coerce").to_numpy()
            ar = pd.to_numeric(ctx[t][f"{t.lower()}_atr_14"], errors="coerce").to_numpy()
            with np.errstate(invalid="ignore", divide="ignore"):
                out.append(np.sign(slope5) * np.sign(sl))
                out.append(ar / atr5)
            names += [f"align_{t.lower()}", f"atr_ratio_{t.lower()}"]
        if not out:
            return pd.DataFrame(index=X0.index)
        return pd.DataFrame(np.vstack(out).T, columns=names)

    arms: dict = {"A0": X0}
    if "A1" in args.arms and "H1" in ctx:
        arms["A1"] = pd.concat([X0, ctx["H1"], cross_feats(["H1"])], axis=1)
    if "A2" in args.arms:
        tags = [t for t in ("M30", "H1", "H4", "D1") if t in ctx]
        arms["A2"] = pd.concat(
            [X0] + [ctx[t] for t in tags] + [cross_feats(tags)], axis=1)
    # A3 = base + 量价/点差（L1 6 列）—— 唯一还没在"**方向**"任务上测过的数据源。
    # 为什么必须补：§7.6 只在"形态"任务上测过 l1（无提升），不能据此断言它对方向无用；
    #   若不补，本节"数据源已测尽"的结论就**不成立**（会犯以偏概全）。
    if "A3" in args.arms:
        l1_cols = list(SF.STATE_FEATURE_COLS_L1)
        miss_l1 = [c for c in l1_cols if c not in df.columns]
        if miss_l1:
            print(f"[warn] CSV 缺 L1 列（{miss_l1}）→ A3 跳过", file=sys.stderr)
        else:
            arms["A3"] = df[l1_cols].astype(float).reset_index(drop=True)

    targets = [("T1 方向", y_dir, None), ("T2 延续", y_cont, None), ("T6 TP先到", y_tp, m6)]
    print(f"\n=========== 方向类：加多周期上下文能否抬 AUC（未来 {n} bar）===========")
    print(f"  {'目标':<12}{'臂':<5}{'特征数':>8}{'n':>7}{'AUC':>9}{'准确率':>9}"
          f"{'多数类基线':>11}{'ΔAUC':>9}   判读")
    lever_found = False
    for tname, y, mask in targets:
        base_auc = None
        for arm, X in arms.items():
            if mask is not None:
                Xa, ya = X[mask].reset_index(drop=True), y[mask]
            else:
                Xa, ya = X, y
            auc, acc, bl, nn = oof_auc(Xa, ya, args.splits)
            if arm == "A0":
                base_auc = auc
            d = "—" if base_auc is None or not np.isfinite(base_auc) or not np.isfinite(auc) \
                else f"{auc - base_auc:+.4f}"
            tag = ""
            if arm != "A0" and np.isfinite(auc) and base_auc is not None and np.isfinite(base_auc):
                if (auc - base_auc) >= DELTA_LEVER and auc >= AUC_FLOOR:
                    tag, lever_found = "**有杠杆**", True
                else:
                    tag = "无杠杆"
            print(f"  {tname:<12}{arm:<5}{X.shape[1]:>8}{nn:>7}{auc:>9.4f}{acc:>9.3f}"
                  f"{bl:>11.3f}{d:>9}   {tag}")

    print("\n=========== 结论 ===========")
    print(f"  · 判据（写死）：ΔAUC ≥ +{DELTA_LEVER} 且 AUC ≥ {AUC_FLOOR} ⇒ 有杠杆。")
    if lever_found:
        print("  · **有杠杆** ⇒ 方向问题应转向「多周期共振」条件，而非单周期模型。")
    else:
        print("  · **封口**：加满 M30/H1/H4/D1 上下文**也不能**把方向 AUC 抬到可用区间")
        print("    ⇒ 「未来 12 bar 方向不可预测」是**数据属性**，与特征集/模型无关。")
    print("  · 口径：因果对齐（高周期只取 close_time ≤ t 的最后一根收盘 bar）⇒ 无泄露；"
          "回放口径，不含成本。")
    print("  · 限制：M5 仅 11 个月（2025-10 起）限制了样本量；高周期虽有 8 年历史但被 M5 窗口截断。")


if __name__ == "__main__":
    main()
