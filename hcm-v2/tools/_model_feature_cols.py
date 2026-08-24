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
    # 【DeepSeek 特征 2026-08-24 纳入契约】ds_* 三列由 quality_features 产出、
    # 推理侧 build_features 从 Redis ai:ds:out:{sym} 读取；此前只在 _model_feature_cols
    # 缺失导致 reindex 丢弃、模型从未吸收 ds 语义（ds_diag 永不触发、裁判拿不到
    # ds_nonzero_ratio）。现纳入 34-36 维，训练 reindex 保留、ds_diag 生效。
    # 缺省 0.0 语义：与推理侧无 ds 票时恒 0 一致，保证训练-推理同分布。
    "ds_fake_prob",
    "ds_sl_coeff",
    "ds_continuity",
]
