"""eval_state_separability.py — 四类状态到底"学到了"还是"学不动"？

问题（用户 2026-09-15）："LightGBM 状态机是否已学习四类状态的判别方法？"

要回答它，必须把两件事分开：
  (a) **模型没学好**：四类可分，但多头模型的决策/训练把它浪费了 → 改判定规则或训练方式即可；
  (b) **类别不可分**：两类的**特征分布本就重叠** → 改模型无用，必须改标签口径或补特征。

方法：对 4 类的**全部 6 个两两组合**各训一个二分类器（时序 OOF，禁泄露），看 AUC。
  · 若某对 AUC ≈ 0.5 → 该对在当前特征+标签下**不可分** → 属 (b)
  · 若所有对 AUC 都明显 > 0.5，而 4 类 macro F1 却很低 → 属 (a)

同时打印**条件预测分布** P(pred=X | true=Y)（行归一化混淆矩阵）：这是最直观的
"模型把哪些类当成同一类"的证据 —— 两行几乎相同 ⇒ 模型无法区分这两类。

用法：
    python eval_state_separability.py --labels _scratch/state_M5_v2.csv
    python eval_state_separability.py --labels ... --tf M5 --splits 5
"""
from __future__ import annotations

import argparse
import importlib.util
import itertools
import os
import sys

import numpy as np
import pandas as pd
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
    spec.loader.exec_module(mod)
    return mod


SF = _load("state_features", os.path.join(_SIG, "state_features.py"))
TSM = _load("train_state_model", os.path.join(_TOOLS, "train_state_model.py"))

# ── 【2026-09-19 D1】「未来度量」特征集（**刻意作弊**，禁止进任何生产路径）──────────
# 来源：`tools/build_state_labels.window_metrics` 落到 CSV 的**未来窗口**度量
#   （`er/disp` 覆盖 i+1..i+n；`er1/disp1` 覆盖 i+1..i+mid；`er2/disp2` 覆盖 i+mid..i+n）。
# 为什么需要这一集：`classify_label` 的判类输入**就是这些列** ⇒ 拿它们当特征等于把答案
#   喂给模型。其**唯一用途**是量化两件事：
#     ① 标签的"未来决定程度"（oracle ≈ 满分 ⇒ 标签是未来的函数，不是"当下状态"）；
#     ② 与 base(27 维过去特征) 的差距 = **过去信息的上限**（决定"补特征"还有没有空间）。
# 纪律：本集无配置键、无推理消费点，只在本脚本内以 --feature-set oracle 显式选择。
ORACLE_COLS = [
    "er", "disp", "er1", "disp1", "er2", "disp2",
    "adx_t", "adx_slope", "mae_atr", "mfe_atr", "new_ext_dir", "delta",
    "er2_f", "mae_f", "adx_slope_f", "new_ext_f",
]


def resolve_feature_cols(name: str, df) -> list:
    """特征集解析（唯一处），缺列 fail-fast（防静默错列）。

    `mem` / `mem_only` 的列名**从构造器模块取**（`build_state_memory_features` 是镜像列的
    唯一实现处）—— 刻意不在此复制清单，否则两处清单会漂移。
    """
    if name == "oracle":
        cols = list(ORACLE_COLS)
    elif name == "l1":
        cols = list(SF.STATE_FEATURE_COLS_L1)
    elif name in ("mem", "mem_only"):
        MEM = _load("build_state_memory_features",
                    os.path.join(_TOOLS, "build_state_memory_features.py"))
        cols = list(MEM.MEM_FEATURE_COLS)
        if name == "mem":
            cols = list(SF.STATE_FEATURE_COLS) + cols
    else:
        cols = list(SF.STATE_FEATURE_COLS)
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise SystemExit(f"[fatal] 特征集 {name} 缺列：{missing}")
    return cols


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="_scratch/state_M5_v2.csv")
    ap.add_argument("--label-col", default="label_id",
                    help="标签列名（默认 label_id；数据驱动状态用 state_id）")
    ap.add_argument("--splits", type=int, default=5, help="TimeSeriesSplit 折数")
    ap.add_argument("--feature-set", default="base",
                    choices=["base", "l1", "oracle", "mem", "mem_only"],
                    help="base = STATE_FEATURE_COLS(27，现行推理契约)；"
                         "l1 = base + 量价/点差(6)；"
                         "oracle = 标签自身的未来度量（作弊集，仅用于量化「标签有多少是"
                         "未来决定的」与「过去信息的上限」，禁止进任何生产路径）")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    import lightgbm as lgb

    df = pd.read_csv(args.labels)
    cols = resolve_feature_cols(args.feature_set, df)
    if args.label_col not in df.columns:
        raise SystemExit(f"[fatal] 缺标签列 {args.label_col}")
    df = df[df[args.label_col].notna()].copy()
    df["label_id"] = df[args.label_col].astype(int)
    df = df.sort_values("open_time").reset_index(drop=True)
    names = TSM.STATE_NAMES
    X = df[cols].astype(float)
    y = df["label_id"].to_numpy()

    print(f"[data] 样本 {len(df)}  feature_set={args.feature_set}({len(cols)}维)  分布 "
          f"{ {names[i]: int((y == i).sum()) for i in range(len(names))} }")

    def mk():
        return lgb.LGBMClassifier(objective="binary", random_state=42, n_jobs=-1,
                                  verbose=-1, **TSM.DEFAULTS)

    # ── 1) 全部 6 个两两组合的可分性（时序 OOF AUC）──
    print("\n=========== 两两可分性（时序 OOF AUC；≈0.5 = 不可分）===========")
    print(f"  {'组合':<26}{'n':>7}{'AUC':>9}{'准确率':>10}{'多数类基线':>12}{'Δ准确率':>10}")
    pairs = {}
    for i, j in itertools.combinations(range(len(names)), 2):
        m = (y == i) | (y == j)
        Xs = X[m].reset_index(drop=True)
        ys = (y[m] == j).astype(int)
        tss = TimeSeriesSplit(n_splits=args.splits)
        oof = np.full(len(ys), np.nan)
        for tr, te in tss.split(Xs):
            mod = mk()
            mod.fit(Xs.iloc[tr], ys[tr])
            oof[te] = mod.predict_proba(Xs.iloc[te])[:, 1]
        cov = ~np.isnan(oof)
        auc = float(roc_auc_score(ys[cov], oof[cov]))
        acc = float(((oof[cov] > 0.5).astype(int) == ys[cov]).mean())
        base = float(max(ys[cov].mean(), 1 - ys[cov].mean()))
        tag = f"{names[i]} vs {names[j]}"
        pairs[tag] = auc
        print(f"  {tag:<26}{int(cov.sum()):>7}{auc:>9.4f}{acc:>10.4f}{base:>12.4f}"
              f"{acc - base:>+10.4f}")

    # ── 2) 条件预测分布 P(pred=X | true=Y)：谁被当成了同一类 ──
    # 用 OOF（TimeSeriesSplit）整体预测，避免用训练集内预测自吹。
    tss = TimeSeriesSplit(n_splits=args.splits)
    oof_mc = np.full((len(y), len(names)), np.nan)
    for tr, te in tss.split(X):
        mod = lgb.LGBMClassifier(objective="multiclass", num_class=len(names),
                                 random_state=42, n_jobs=-1, verbose=-1,
                                 **TSM.DEFAULTS)
        mod.fit(X.iloc[tr], y[tr])
        oof_mc[te] = mod.predict_proba(X.iloc[te])
    cov = ~np.isnan(oof_mc[:, 0])
    yc, pc = y[cov], oof_mc[cov].argmax(axis=1)

    print("\n=========== 条件预测分布 P(pred | true)，行归一化 ===========")
    hdr = "".join(f"{n[:10]:>12}" for n in names)
    print(f"  {'true \\ pred':<14}{hdr}{'n':>8}")
    rows = {}
    for i in range(len(names)):
        m = yc == i
        if m.sum() == 0:
            continue
        row = np.array([float((pc[m] == j).mean()) for j in range(len(names))])
        rows[names[i]] = row
        print(f"  {names[i]:<14}" + "".join(f"{v:>12.1%}" for v in row) + f"{int(m.sum()):>8}")

    # ── 3) 判据：行间差异（两行越接近 ⇒ 模型无法区分这两类）──
    print("\n=========== 判据 ===========")
    ks = [k for k in rows if rows[k] is not None]
    worst = None
    for i, j in itertools.combinations(ks, 2):
        tv = float(np.abs(rows[i] - rows[j]).sum() / 2.0)   # 总变差距离
        if worst is None or tv < worst[0]:
            worst = (tv, i, j)
        print(f"  {i} vs {j:<14} 条件分布总变差 = {tv:.1%}"
              + ("  ← 几乎同分布，模型**无法区分**" if tv < 0.10 else ""))
    mid_pairs = [a for a, b in pairs.items() if 0.5 < b < 0.62]
    print(f"\n  AUC 落在 0.5~0.62（≈不可分）的组合：{mid_pairs or '无'}")
    if worst:
        print(f"  最接近的两类：{worst[1]} / {worst[2]}（总变差 {worst[0]:.1%}）")
    print("\n  判据读法：若关键组合 AUC≈0.5 → 属「类别不可分」(b)，改模型无用，"
          "须改标签口径或补特征；若 AUC 均>0.62 而 4 类 F1 低 → 属「没学好」(a)。")


if __name__ == "__main__":
    main()
