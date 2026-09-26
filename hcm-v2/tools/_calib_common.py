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


def calib_is_degenerate(calib, min_levels: int = 8, min_span: float = 0.10,
                        min_iqr: float = 0.15, grid_n: int = 17) -> bool:
    """校准器退化判定 —— **训练/验收侧与生产 quality_scorer._calib_is_degenerate
    完全同口径**（y_fit 档位计数 + 17 点决策网格唯一值 + 输出跨度 + IQR 四判据）。

    为什么放这里（2026-09-25 根因修复）：train_signal_quality.py 的 `[calib-final]`
    判据与生产侧**口径不一致**（probe×101/round-6 vs y_fit/round-4 + 网格四判据），
    同一校准器训练侧判退化、生产侧放行/ vice versa ⇒ auto_retrain 的
    `quality_calib_degenerate` 误判（v109 实测：OOF isotonic 8 阈值，probe 口径仅
    7 档 → 判 DEGENERATE 拒收整轮）。统一以本函数为准，训练侧验收不再漂移。

    生产实现对 PlattCalibrator **无分支**（y_thresholds_/y_fit 皆无 ⇒ 恒 False，
    属既有漏洞）；本函数补 Platt 专属分支（level_count + 网格跨度），训练侧验收
    **不依赖**该漏洞。判定失败一律返回 False（与生产一致：宁可漏判不让校准器
    被误杀后静默回退 raw）。
    """
    try:
        _min = int(min_levels)
        # 分支 1：sklearn IsotonicRegression
        yt = getattr(calib, "y_thresholds_", None)
        if yt is not None:
            return len({round(float(v), 4) for v in yt}) < _min
        # 分支 2：calib_np.NumpyCalibrator —— y_fit 档位 + 决策网格三判据
        yf = getattr(calib, "y_fit", None)
        if yf is not None:
            levels = {round(float(v), 4) for v in yf}
            if len(levels) < _min:
                return True
            grid = np.linspace(0.1, 0.9, int(grid_n))
            outs = np.round(np.asarray(calib.predict(grid), dtype=float), 4)
            if len(set(outs.tolist())) < _min:
                return True
            if float(outs.max() - outs.min()) < float(min_span):
                return True
            _q1, _q3 = np.percentile(outs, [25, 75])
            if float(_q3) - float(_q1) < float(min_iqr):
                return True
            return False
        # 分支 3（补生产漏洞）：calib_np.PlattCalibrator —— 连续输出无阶梯
        _lc = getattr(calib, "level_count", None)
        if callable(_lc):
            try:
                _n = int(_lc(100))
            except TypeError:
                _n = int(_lc())
            if _n < 2:          # a≈0 ⇒ 输出近常数 ⇒ 退化
                return True
            grid = np.linspace(0.1, 0.9, int(grid_n))
            outs = np.asarray(calib.predict(grid), dtype=float)
            if float(outs.max() - outs.min()) < float(min_span):
                return True
            return False
        return False
    except Exception:
        return False
