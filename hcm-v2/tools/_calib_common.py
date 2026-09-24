#!/usr/bin/env python3
"""_calib_common.py — 【2026-09-21 去重】校准评估/拟合函数的**唯一真源**。

存在理由（为什么必须去重）：
  `recalibrate_quality.py`（质量头/方向头/买点头链路）与
  `review_recalibrate.py`（评审器链路）此前**各自复制了一份**同名实现：
      _ece / _monotonicity / _fit_platt
  （2026-09-21 逐行比对：三者在两处**逐字节相同**）

  两条链本应只在"数据源 + 写盘目标"上不同 ——
    · 质量链：`hcm_ai.ai_pred_raw` → `ai.lm.*_calib_path`
    · 评审链：`hcm_ai.review_log`  → `models/review_*/calib_review_*.pkl`
  但**校准质量的评估口径必须一致**：否则同一份 `labels.csv` 真值会在两条链上
  得出不同的 ECE / 单调性结论，而 `ai_health` 的两个 calib_health 键呈现的差异
  将无法归因（是数据差还是实现差？）。这正是本会话"重叠项"清单里
  「基础设施重复 ④-1」的处置。

纪律：本文件是这三个函数的唯一真源。任何调用方一律 import，禁止再复制副本。
回滚：把函数体复制回各自脚本即可（两链互不影响，可单独回退）。
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd

# 与两个调用脚本同一纪律：本文件所在目录（tools/）须在 sys.path 上，才能取到 calib_np。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from calib_np import PlattCalibrator  # noqa: E402

__all__ = ["PlattCalibrator", "ece", "monotonicity", "fit_platt"]


def ece(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    """期望校准误差（等宽分箱）。用于"校准器是否需要重拟合"的门槛判定。"""
    edges = np.linspace(0.0, 1.0, bins + 1)
    tot = max(len(y), 1)
    e = 0.0
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & (p < hi if hi < 1.0 else p <= hi)
        if m.sum() == 0:
            continue
        e += (m.sum() / tot) * abs(float(p[m].mean()) - float(y[m].mean()))
    return float(e)


def monotonicity(y: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    """分位分箱「预测均值 vs 实测胜率」的皮尔逊相关。

    这是"分层是否可用"的核心判据：≤0 表示高置信反而更差 ⇒ 必须拒绝替换校准器。
    """
    q = pd.qcut(pd.Series(p), bins, labels=False, duplicates="drop")
    xs, ys = [], []
    for b in sorted(pd.unique(q.dropna())):
        m = (q == b).values
        if m.sum() < 3:
            continue
        xs.append(float(p[m].mean()))
        ys.append(float(y[m].mean()))
    if len(xs) < 3:
        return 0.0
    return float(np.corrcoef(xs, ys)[0, 1])


def fit_platt(raw: np.ndarray, y: np.ndarray) -> PlattCalibrator:
    """Platt 标定：在 logit 上做近无正则 logistic 回归（C=1e6）。"""
    from sklearn.linear_model import LogisticRegression
    _eps = 1e-6
    _p = np.clip(np.asarray(raw, float), _eps, 1.0 - _eps)
    z = np.log(_p / (1.0 - _p)).reshape(-1, 1)
    lr = LogisticRegression(C=1e6, solver="lbfgs", max_iter=2000)
    lr.fit(z, np.asarray(y).astype(int))
    return PlattCalibrator(float(lr.coef_[0][0]), float(lr.intercept_[0]))
