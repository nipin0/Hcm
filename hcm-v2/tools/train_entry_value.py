#!/usr/bin/env python3
"""train_entry_value.py — Step-2.3「前瞻价值头」训练（纯离线研究，零实盘影响）。

读 build_path_labels.py 的标签集（全网格候选点，label=实现 R）。
LightGBM 回归 E[R]；时间序 walk-forward 切分；评估「预测分位 → 真实 E[R] 单调性」
与 Safe-rule（rise_atr≤0.5 & dist_h1e_atr≤0.5）基线对比。

若价值头预测分位能单调拉开真实 E[R]、且高分位明显优于 Safe rule → 说明模型
可学会"安全点"，比手工规则更强 → 值得接入影子（Step-2.4）。

用法:
  cd tools
  python train_entry_value.py --labels _artifacts/labels_path.csv \
      --model _artifacts/lgbm_value_v1.txt
"""
from __future__ import annotations

import argparse
import sys

import numpy as np
import pandas as pd
from scipy import stats as _sps

import lightgbm as lgb

EXCLUDE = {"symbol", "open_time", "label", "close"}
CLIP = 2.5           # 长尾(time-out)截断，避免少数极端值支配回归


def _fit_and_score(Xtr, ytr, Xva, yva, seed=42):
    m = lgb.LGBMRegressor(
        objective="huber", n_estimators=600, learning_rate=0.05,
        num_leaves=15, min_child_samples=40, subsample=0.8, colsample_bytree=0.8,
        random_state=seed, verbose=-1,
    )
    m.fit(Xtr, ytr, eval_set=[(Xva, yva)],
          callbacks=[lgb.early_stopping(80, verbose=False)])
    return m, m.predict(Xva)


def _roll_windows(n, k):
    """时间序上把 [0,n) 均分 k 段；返回 [(train_idx, test_idx)]（train=test 前的全部）。"""
    edges = [int(n * i / k) for i in range(k + 1)]
    return [(list(range(0, edges[i])), list(range(edges[i], edges[i + 1])))
            for i in range(1, k)]


def run_roll(df, feats, y, args):
    """A. 多窗口滚动 walk-forward 稳健性：价值头 top20% 是否每窗都显著优于全体/为正。"""
    wins = _roll_windows(len(df), args.roll_k)
    rows = []
    for wi, (tr_idx, te_idx) in enumerate(wins, 1):
        Xnum = df[feats].apply(pd.to_numeric, errors="coerce")
        med = Xnum.iloc[tr_idx].median()          # 仅用训练集中位数填充（防泄漏）
        Xtr, Xte = Xnum.iloc[tr_idx].fillna(med), Xnum.iloc[te_idx].fillna(med)
        ytr, yte = y.iloc[tr_idx], y.iloc[te_idx]
        if len(Xtr) < 1000 or len(Xte) < 200:
            continue
        m, p = _fit_and_score(Xtr, ytr, Xte, yte, args.seed)
        tail = df.iloc[te_idx].reset_index(drop=True)
        allm = yte.to_numpy()
        top = p >= np.quantile(p, 0.8)
        bot = p <= np.quantile(p, 0.2)
        safe = ((tail["rise_atr"] <= 0.5) & (tail["dist_h1e_atr"] <= 0.5)).to_numpy()
        t0, t1 = df["open_time"].iloc[te_idx[0]], df["open_time"].iloc[te_idx[-1]]
        rows.append({
            "win": wi, "test_win": f"{t0:%m-%d}~{t1:%m-%d}", "n": len(allm),
            "all_ER": allm.mean(), "all_wr": np.mean(allm > 0),
            "top_ER": allm[top].mean(), "top_wr": np.mean(allm[top] > 0),
            "bot_ER": allm[bot].mean(), "safe_ER": allm[safe].mean(),
            "safe_n": int(safe.sum()),
        })
    res = pd.DataFrame(rows)
    print("\n== A. 滚动 walk-forward 稳健性（K=%d 窗）==" % args.roll_k)
    fmt = res.copy()
    for c in ("all_ER", "top_ER", "bot_ER", "safe_ER"):
        fmt[c] = fmt[c].map(lambda v: f"{v:+.3f}")
    fmt["all_wr"] = fmt["all_wr"].map(lambda v: f"{v:.2f}")
    fmt["top_wr"] = fmt["top_wr"].map(lambda v: f"{v:.2f}")
    print(fmt[["win", "test_win", "n", "all_ER", "top_ER", "bot_ER", "safe_ER",
               "safe_n", "all_wr", "top_wr"]].to_string(index=False))
    good = ((res["top_ER"] > 0) & (res["top_ER"] > res["all_ER"])).mean()
    print(f"\n[结论] top20% 在 {len(res)} 窗中正期望且优于全体的比例 = {good:.0%}")
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="_artifacts/labels_path.csv")
    ap.add_argument("--model", default="_artifacts/lgbm_value_v1.txt")
    ap.add_argument("--test-ratio", type=float, default=0.30)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--roll", action="store_true", help="滚动多窗口 walk-forward 稳健性")
    ap.add_argument("--roll-k", type=int, default=5)
    args = ap.parse_args()

    df = pd.read_csv(args.labels)
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    df = df.sort_values("open_time").reset_index(drop=True)
    y = df["label"].clip(-CLIP, CLIP)
    feats = [c for c in df.columns if c not in EXCLUDE and c in df.columns]
    print(f"[data] rows={len(df)}  features={len(feats)}  E[label]={y.mean():+.4f}")
    if args.roll:
        run_roll(df, feats, y, args)
        return

    X = df[feats].copy()
    for c in X.columns:
        X[c] = pd.to_numeric(X[c], errors="coerce")
    X = X.fillna(X.median())

    n = len(df)
    cut = int(n * (1 - args.test_ratio))
    Xtr, Xte, ytr, yte = X.iloc[:cut], X.iloc[cut:], y.iloc[:cut], y.iloc[cut:]
    print(f"[split] train={len(Xtr)}  test={len(Xte)}  "
          f"(test 窗 {df['open_time'].iloc[cut]:%m-%d} ~ {df['open_time'].iloc[-1]:%m-%d})")

    m = lgb.LGBMRegressor(
        objective="huber", n_estimators=600, learning_rate=0.05,
        num_leaves=15, min_child_samples=40, subsample=0.8, colsample_bytree=0.8,
        random_state=args.seed, verbose=-1,
    )
    m.fit(Xtr, ytr, eval_set=[(Xte, yte)],
          callbacks=[lgb.early_stopping(80, verbose=False)])
    p = m.predict(Xte)

    # 预测分位单调性（测试集）
    te = pd.DataFrame({"pred": p, "act": yte.to_numpy()})
    te["bucket"] = pd.qcut(te["pred"], 5, labels=["Q1低", "Q2", "Q3", "Q4", "Q5高"])
    print("\n== 测试集：预测分位 → 真实 E[R]（价值头单调性）==")
    g = te.groupby("bucket", observed=True).agg(n=("act", "size"), E_R=("act", "mean"),
                                                wr=("act", lambda s: np.mean(s > 0)))
    g["E_R"] = g["E_R"].map(lambda v: f"{v:+.4f}")
    g["wr"] = g["wr"].map(lambda v: f"{v:.3f}")
    print(g.to_string())
    try:
        rho, pv = _sps.spearmanr(te["pred"], te["act"])
        print(f"\n[spearman] rho={rho:.4f} p={pv:.2e}  (测试集 {len(te)} 样本)")
    except Exception as e:
        print(f"[warn] spearman failed: {e}")

    # 基线对比（test 窗内真实 E[label]）
    tail = df.iloc[cut:].reset_index(drop=True)
    m_safe = (tail["rise_atr"] <= 0.5) & (tail["dist_h1e_atr"] <= 0.5)
    m_confirm = (tail["rise_atr"] > 0.5) & (tail["rise_atr"] <= 2.0) & \
                (tail["dist_h1e_atr"] > 0.0) & (tail["dist_h1e_atr"] <= 0.5)
    q5 = te["pred"] >= np.quantile(te["pred"], 0.8)   # 价值头 top20%
    print("\n== 测试集：策略对比（真实 E[label]）==")
    for name, msk in [("全体顺向", np.ones(len(tail), bool)),
                      ("Safe rule(贴低位)", m_safe.to_numpy()),
                      ("Confirm rule(确认追高)", m_confirm.to_numpy()),
                      ("价值头 top20%", q5.to_numpy())]:
        sub = yte.to_numpy()[msk]
        if len(sub) == 0:
            print(f"{name:22s} n=0")
            continue
        print(f"{name:22s} n={len(sub):5d}  E[R]={sub.mean():+.4f}  "
              f"wr={np.mean(sub > 0):.3f}  med={np.median(sub):+.3f}")

    imp = pd.Series(m.feature_importances_, index=feats).sort_values(ascending=False)
    print("\n== 特征重要性 top15 ==")
    print(imp.head(15).to_string())
    print(f"[save] {args.model}  (n_estimators={m.best_iteration_ or 'n/a'})")
    try:
        m.booster_.save_model(args.model)
    except Exception as e:
        print(f"[warn] model save failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
