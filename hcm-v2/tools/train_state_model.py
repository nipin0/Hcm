#!/usr/bin/env python3
"""train_state_model.py — 行情状态模型（4 类）训练器。

依据：docs/设计方案_信号塔状态机与交易策略重构_20260914.md §4

要点（严格对齐方案 §4）：
  * 目标 = 4 分类（multi_logloss）。**不使用 lambdarank**：它面向排序任务、需要 query
    group，与本处的多分类目标不符（方案 §13 差异清单已记录该纠正）。
  * 类别权重：balanced 权重 × 配置 boost（默认给 trend_fade 加权，提升其召回 —— 需求二.3）。
  * 验证：TimeSeriesSplit（**禁止普通 k-fold**）—— 时序切分避免未来泄露，与
    train_signal_quality.oof_proba_time_series 同范式。
  * 集成：bagging —— K 个随机种子各训一份，推理侧取概率平均后再 argmax（需求二.4）。
  * 产物：K 个模型文件 + meta.json（含特征列顺序 / 类别顺序 / 训练区间 / CV 指标）。

纪律：本脚本**只读 CSV、只写 models 目录**，不触碰 PG/Redis/生产配置。

用法：
  python train_state_model.py --csv state_M5.csv --tf M5 --version 1 --seeds 5
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

try:
    import lightgbm as lgb
except Exception as exc:  # pragma: no cover
    print(f"[fatal] lightgbm unavailable: {exc}", file=sys.stderr)
    raise

from sklearn.metrics import (
    classification_report, confusion_matrix, f1_score,
    precision_recall_fscore_support, roc_auc_score,
)
from sklearn.model_selection import TimeSeriesSplit
from sklearn.utils.class_weight import compute_class_weight

MODELS_DIR_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")

# 类别顺序即 label_id（与 build_state_labels.STATE_NAMES 严格一致，契约）
#
# 【2026-09-15 定案】本训练器**只训 4 类行情形态**：
#  · 形态：本文件（LGBM、前视标签、防抖后驱动 FSM 状态）。
#  · 方向：**规则模块** signal_tower/trend_direction.py（ATR 归一斜率 + ±DI + K 线防抖），
#    不在此训练 —— 方向若也建模型即形成第二份方向真值，与"同一语义只允许一份实现"红线冲突。
# 否决过的两条路（均有实测依据，见方案 §19/§20）：
#  · 7 类合并（形态×方向并入一个模型）：M5 方向准确率 0.521≈随机，形态 F1 还退化 1.2pt；
#  · 独立 3 类方向模型：M5 方向可用 AUC 0.5186（低于多数类基线）→ 不可学。
STATE_NAMES = ["oscillation", "trend_init", "trend_mid", "trend_fade"]

# 训练默认超参（禁魔法数字：逐项注明用途；生产以 args/配置覆盖）
DEFAULTS = {
    "n_estimators": 400,
    "learning_rate": 0.05,
    "num_leaves": 31,
    "min_child_samples": 20,
    "subsample": 0.85,
    "subsample_freq": 1,
    "colsample_bytree": 0.85,
    "reg_lambda": 1.0,
    "max_depth": -1,
}
# 类别权重 boost（需求二.3：提升 trend_fade 召回；键=类别名）
CLASS_WEIGHT_BOOST_DEFAULT = {"oscillation": 1.0, "trend_init": 1.0,
                              "trend_mid": 1.0, "trend_fade": 1.5}


def load_csv(path: str, cols: list[str]) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise SystemExit(f"[fatal] csv missing feature columns: {missing}")
    if "label_id" not in df.columns:
        raise SystemExit("[fatal] csv missing label_id")
    df = df[df["label_id"].notna()].copy()
    df["label_id"] = df["label_id"].astype(int)
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    return df.sort_values("open_time").reset_index(drop=True)


def class_weights(y: np.ndarray, boost: dict) -> dict:
    present = np.unique(y)
    w = compute_class_weight("balanced", classes=present, y=y)
    out = {}
    for cls, wc in zip(present, w):
        name = STATE_NAMES[int(cls)]
        out[int(cls)] = float(wc) * float(boost.get(name, 1.0))
    return out


def build_model(seed: int) -> lgb.LGBMClassifier:
    """按种子构造分类器（类别权重经 sample_weight 显式传入，不用 class_weight 参数）。"""
    return lgb.LGBMClassifier(
        objective="multiclass",
        num_class=len(STATE_NAMES),
        random_state=seed,
        n_jobs=-1,
        verbose=-1,
        **DEFAULTS,
    )


def sample_weight_for(cw: dict, labels: np.ndarray) -> np.ndarray:
    return np.asarray([cw.get(int(c), 1.0) for c in labels])


def oof_time_series(X: pd.DataFrame, y: np.ndarray, n_splits: int, cw: dict):
    """时序 OOF 概率（手写循环：TimeSeriesSplit 首段不进测试集，cross_val_predict 会报错）。"""
    tss = TimeSeriesSplit(n_splits=n_splits)
    oof = None
    for tr, te in tss.split(X):
        m = build_model(42)
        m.fit(X.iloc[tr], y[tr], sample_weight=sample_weight_for(cw, y[tr]))
        p = m.predict_proba(X.iloc[te])
        if oof is None:
            oof = np.full((len(X), len(STATE_NAMES)), np.nan)
        oof[te] = p
    if oof is None:
        return None, None
    cov = ~np.isnan(oof[:, 0])
    return oof[cov], y[cov]


def report(tag: str, y_true: np.ndarray, proba: np.ndarray) -> dict:
    pred = proba.argmax(axis=1)
    labels = list(range(len(STATE_NAMES)))
    macro_f1 = f1_score(y_true, pred, average="macro", labels=labels, zero_division=0)
    pr, rc, f1, sup = precision_recall_fscore_support(
        y_true, pred, labels=labels, zero_division=0)
    try:
        auc_ovr = roc_auc_score(y_true, proba, multi_class="ovr", average="macro",
                                labels=labels)
    except ValueError:
        auc_ovr = float("nan")

    print(f"\n===== {tag} =====")
    print(f"  samples={len(y_true)}  macro_F1={macro_f1:.4f}  AUC_OVR(宏观)={auc_ovr:.4f}")
    print(f"  {'class':<14}{'precision':>10}{'recall':>10}{'f1':>10}{'support':>10}")
    per_class = {}
    for c in labels:
        nm = STATE_NAMES[c]
        print(f"  {nm:<14}{pr[c]:>10.4f}{rc[c]:>10.4f}{f1[c]:>10.4f}{int(sup[c]):>10d}")
        per_class[nm] = {"precision": float(pr[c]), "recall": float(rc[c]),
                         "f1": float(f1[c]), "support": int(sup[c])}
    print("  confusion matrix (row=true, col=pred):")
    cm = confusion_matrix(y_true, pred, labels=labels)
    header = "".join(f"{n[:9]:>11}" for n in STATE_NAMES)
    print(f"  {'':<14}{header}")
    for c in labels:
        row = "".join(f"{int(v):>11d}" for v in cm[c])
        print(f"  {STATE_NAMES[c]:<14}{row}")

    return {"macro_f1": float(macro_f1), "auc_ovr_macro": float(auc_ovr),
            "per_class": per_class, "confusion": cm.tolist(),
            "samples": int(len(y_true))}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="build_state_labels.py 的产物")
    ap.add_argument("--feature-set", default="base", choices=["base", "l1"],
                    help="特征集：base = STATE_FEATURE_COLS(27)；"
                         "l1 = base + L1_FEATURE_COLS（量价/点差 6 列）。"
                         "默认 base（与既有模型及推理契约一致，零变化）")
    ap.add_argument("--tf", required=True, choices=["M5", "M15", "H1"])
    ap.add_argument("--version", type=int, required=True, help="模型版本号（vN）")
    ap.add_argument("--seeds", type=int, default=5, help="bagging 随机种子数 K")
    ap.add_argument("--cv-splits", type=int, default=5, help="TimeSeriesSplit 折数")
    ap.add_argument("--test-ratio", type=float, default=0.2, help="末端留出测试比例")
    ap.add_argument("--outdir", default=MODELS_DIR_DEFAULT)
    ap.add_argument("--report", default=None, help="评估报告输出路径（json）")
    args = ap.parse_args()

    # 特征列顺序从特征契约导入（单一真值）
    feat_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "hcm-signal-tower", "signal_tower", "state_features.py")
    spec = importlib.util.spec_from_file_location("state_features", feat_path)
    sf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sf)
    cols = list(sf.STATE_FEATURE_COLS_L1 if args.feature_set == "l1"
                else sf.STATE_FEATURE_COLS)

    df = load_csv(args.csv, cols)
    if len(df) < 200:
        print(f"[warn] 样本仅 {len(df)} 条，指标不可靠（需积累更多 K 线历史）", file=sys.stderr)
    if df["label_id"].nunique() < 2:
        raise SystemExit("[fatal] 标签类别不足 2 类，检查标签阈值标定")

    X = df[cols].astype(float)
    y = df["label_id"].to_numpy()
    cw = class_weights(y, CLASS_WEIGHT_BOOST_DEFAULT)

    print(f"[data] rows={len(df)} tf={args.tf} "
          f"range={df['open_time'].iloc[0]} .. {df['open_time'].iloc[-1]}")
    print(f"[dist] {df['label_name'].value_counts().to_dict()}")
    print(f"[class_weight] { {STATE_NAMES[k]: round(v, 3) for k, v in cw.items()} }")

    # ── 评估 1：全量时序 OOF（最接近线上口径）──
    oof_p, oof_y = oof_time_series(X, y, args.cv_splits, cw)
    if oof_p is None:
        raise SystemExit("[fatal] OOF 失败（样本不足或类别缺失）")
    oof_metrics = report(f"TimeSeriesSplit OOF ({args.cv_splits} folds)", oof_y, oof_p)

    # ── 评估 2：末端留出测试集（bagging 平均口径 = 线上推理口径）──
    # 注意：留出集模型只能用训练段拟合并评估；这与下面的"全量最终模型"是两批不同的模型。
    split = int(len(df) * (1.0 - args.test_ratio))
    Xtr, Xte = X.iloc[:split], X.iloc[split:]
    ytr, yte = y[:split], y[split:]
    proba_te = np.zeros((len(Xte), len(STATE_NAMES)))
    for s in range(args.seeds):
        m = build_model(100 + s)
        m.fit(Xtr, ytr, sample_weight=sample_weight_for(cw, ytr))
        proba_te += m.predict_proba(Xte)
    proba_te /= args.seeds
    holdout_metrics = report(
        f"Holdout last {args.test_ratio:.0%} (bagging x{args.seeds})", yte, proba_te)

    # ── 最终 bagging 模型：全量数据训 K 个种子（保存 + 顺带累计特征重要性）──
    os.makedirs(args.outdir, exist_ok=True)
    paths = []
    imp = np.zeros(len(cols))
    for s in range(args.seeds):
        m = build_model(100 + s)
        m.fit(X, y, sample_weight=sample_weight_for(cw, y))
        name = f"lgbm_state_{args.tf}_v{args.version}_s{s}.txt"
        m.booster_.save_model(os.path.join(args.outdir, name))
        paths.append(name)
        imp += m.feature_importances_
    imp /= args.seeds
    top = sorted(zip(cols, imp), key=lambda t: -t[1])[:12]
    print("\n[top_features] " + ", ".join(f"{c}={v:.0f}" for c, v in top))

    meta = {
        "tf": args.tf,
        "version": args.version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "feature_cols": cols,
        "class_names": STATE_NAMES,
        "n_seeds": args.seeds,
        "model_files": paths,
        "class_weight": {STATE_NAMES[k]: v for k, v in cw.items()},
        "lgbm_params": DEFAULTS,
        "train_rows": int(len(df)),
        "train_range": [df["open_time"].iloc[0].isoformat(),
                        df["open_time"].iloc[-1].isoformat()],
        "eval_oof": oof_metrics,
        "eval_holdout": holdout_metrics,
        "feature_importance": {c: float(v) for c, v in zip(cols, imp)},
    }
    meta_path = os.path.join(args.outdir, f"lgbm_state_{args.tf}_v{args.version}_meta.json")
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    print(f"\n[out] {len(paths)} models + meta → {args.outdir}")
    print(f"[out] meta: {meta_path}")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)
        print(f"[out] report: {args.report}")


if __name__ == "__main__":
    main()
