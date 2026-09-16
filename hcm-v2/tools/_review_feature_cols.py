"""_review_feature_cols.py — 【P1 2026-09-11】信号级评审模型的特征契约（单一真值）。

依据：docs/设计方案_LightGBM解耦信号级评审与校准治理_20260911.md §3.1 / §4.2

根因（本契约存在的理由）：
  现行 quality_scorer 的模型**只吃市场快照**（K线派生指标），信号自身的
  **方向 / 入场价 / 结构位** 完全不作为输入（train_signal_quality.prepare 的
  drop_cols 显式丢弃 signal_dir）。而标签（dir_label / entry_label）却是**条件于
  信号方向**构造的 → 训练对象与推理对象错位（train/serve skew，方案 §2.1）。

本文件定义评审模型权威特征列表，train 侧（tools/review_dataset.py）与
推理侧（hcm-signal-tower/signal_tower/reviewer.py）**必须**共同 import，
严禁各自维护副本（与 _model_feature_cols.py 同一纪律）。
"""

from _model_feature_cols import MODEL_FEATURE_COLS

# ── 信号属性列（推理时由 ReviewInput 从 SignalData 现算；训练时由 review_dataset 同式派生）──
REVIEW_ATTR_COLS = [
    # 信号意图方向：+1=BUY / -1=SELL / 0=未知。
    # 此前模型完全看不到该列 → 无法回答"这个方向到底行不行"，是本方案的核心修复点。
    "dir_sign",
]

# 权威评审特征列表（顺序即入模顺序，LightGBM 按名匹配但此处固定以保 fail-fast）
REVIEW_FEATURE_COLS = list(MODEL_FEATURE_COLS) + REVIEW_ATTR_COLS

__all__ = ["REVIEW_ATTR_COLS", "REVIEW_FEATURE_COLS"]
