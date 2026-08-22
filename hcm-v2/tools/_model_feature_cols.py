"""模型特征列单一真值（数据契约）。

训练侧(train_signal_quality.py)与推理侧(quality_scorer.build_features)必须
产出完全相同的特征集合与顺序——LightGBM 按列名匹配，但为 fail-fast 与
可审计，此处固定权威 33 维列表（P2-T8 经 VIF 诊断裁剪 7 个强冗余列），
双方均从此 import，消除双份数据源。

若推理侧 FEATURE_COLS 调整，必须同步修改本文件并由训练侧 reindex 对齐。
"""

MODEL_FEATURE_COLS = [
    "adx_14",
    "rsi_14",
    "macd",
    "atr_14",
    "plus_di",
    "minus_di",
    "er",
    "bbw",
    "bbw_pct",
    "hurst",
    "mm",
    "ema20_dist_atr",
    "body_ratio",
    "pullback_depth",
    "atr_pct",
    "spread_num",
    "spread_atr",
    "donchian_q",
    "dev_z_ema20",
    "dev_z_ema60",
    "dev_z_ema200",
    "macd_slope3",
    "body_wick_ratio",
    "extreme_reversal",
    "di_ratio",
    "di_net",
    "close_mom_atr",
    "trend_aligned",
    "session_eu",
    "session_us",
    "event_proximity_min",
    "macro_risk_score",
    "sentiment_risk_score",
]
