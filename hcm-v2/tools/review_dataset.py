#!/usr/bin/env python3
"""review_dataset.py — 【P1】信号级评审数据集构建（方案 §3.1 / §4.2）。

在既有产物之上补【信号属性】，产出评审模型的单表训练集：
  labels.csv   （build_labels.py：dir_label / entry_label / label / R）
  features.csv （quality_features.py：36 维市场特征）
    ↓ merge(signal_id) + 派生信号属性
  review_dataset.csv

派生口径与推理侧（signal_tower/reviewer.py）严格同式，保证 train/serve 一致。

用法：
  python review_dataset.py --labels _artifacts/labels.csv \
      --features _artifacts/features.csv --out _artifacts/review_dataset.csv
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import train_signal_quality as ts  # noqa: E402  复用其 load（merge + 派生列）
from _review_feature_cols import REVIEW_ATTR_COLS, REVIEW_FEATURE_COLS  # noqa: E402


def derive_dir_sign(series: pd.Series) -> np.ndarray:
    """信号方向 → ±1（BUY→+1 / SELL→-1 / 其他→0）。

    【2026-09-21 文档修正】原文写"**训练/推理必须调用同一函数**（reviewer.py 亦 import 本函数）"，
    与事实不符：推理侧 `signal_tower/reviewer.py::derive_dir_sign(direction: str)` 是**另写的一份**，
    并未 import 本函数 —— 因为 reviewer 运行在 signal-tower 容器（/app），
    本文件在宿主 tools/，**跨容器无法 import**（`tools/models` 才有 bind mount）。
    ⇒ 这是**架构性重复，不可消除**，只能锁契约。实际契约是"**同式**"而非"同源"：
      两者都做 `strip().upper()` 后 BUY→+1 / SELL→-1 / 其余→0。
    修改任一侧的映射规则时，**必须同步另一侧**（本函数对应 reviewer.py 同名函数）。
    """
    s = series.astype(str).str.upper()
    return np.where(s == "BUY", 1.0, np.where(s == "SELL", -1.0, 0.0))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default="_artifacts/labels.csv")
    ap.add_argument("--features", default="_artifacts/features.csv")
    ap.add_argument("--out", default="_artifacts/review_dataset.csv")
    args = ap.parse_args()

    df = ts.load(args.labels, args.features)
    print(f"[load] merged rows={len(df)} cols={len(df.columns)}", file=sys.stderr)

    # labels/features 两表都含 signal_dir/symbol → merge 后带 _x/_y 后缀（_x=labels，权威方向）
    _sd_col = "signal_dir_x" if "signal_dir_x" in df.columns else "signal_dir"
    if _sd_col not in df.columns:
        raise RuntimeError(f"[fatal] 找不到 signal_dir 列（现有: {list(df.columns)[:12]}...）")
    df["dir_sign"] = derive_dir_sign(df[_sd_col])

    # 标签有效性过滤（与 train_signal_quality.prepare 同口径）
    valid = df["label"].notna()
    dfv = df[valid].reset_index(drop=True)
    print(f"[label] NaN label dropped={int((~valid).sum())} keep={len(dfv)}", file=sys.stderr)

    keep = (["signal_id"] + REVIEW_FEATURE_COLS
            + ["dir_label", "entry_label", "label", "created_at"])
    for c in keep:
        if c not in dfv.columns:
            print(f"[warn] column missing, filled 0.0: {c}", file=sys.stderr)
            dfv[c] = 0.0
    out = dfv[keep].copy()
    # 市场特征缺失 → 0.0（与推理侧 quality_scorer.build_features 恒 0 语义一致）
    for c in REVIEW_FEATURE_COLS:
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    out.to_csv(args.out, index=False)
    print(f"[out] {args.out} rows={len(out)} feats={len(REVIEW_FEATURE_COLS)} "
          f"attrs={REVIEW_ATTR_COLS}")
    print(f"[dist] dir_sign={out['dir_sign'].value_counts().to_dict()} "
          f"entry_label={out['entry_label'].value_counts(dropna=False).to_dict()} "
          f"dir_label={out['dir_label'].value_counts(dropna=False).to_dict()}")


if __name__ == "__main__":
    main()
