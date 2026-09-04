"""模型特征列单一真值（数据契约）。

训练侧(train_signal_quality.py)与推理侧(quality_scorer.build_features)必须
产出完全相同的特征集合与顺序——LightGBM 按列名匹配，但为 fail-fast 与
可审计，此处固定权威 35 维列表，双方均从此 import，消除双份数据源。

若推理侧 FEATURE_COLS 调整，必须同步修改本文件并由训练侧 reindex 对齐。

──────────────────────────────────────────────────────────────────────
【2026-08-31 修订·37 维】修正 A1 的误删（重要教训）
──────────────────────────────────────────────────────────────────────
A1(53→32 维)的裁剪依据是 `live_baseline.json` 声称"某特征生产恒 0"。
但事后用推理侧真实装配值 `lm_features`（sidecar 发布到 Redis
hcm:live:hexp:ai:{sym} 的实际入模特征）交叉验证，发现 **live_baseline 失真**：
    live_baseline 声称 adx_14=44.8 恒定 → 实际 18.4（有变化）
    live_baseline 声称 atr_14=4.18 恒定 → 实际 7.49
    live_baseline 声称 ds_* 恒 0     → 实际 0.62/1.2/45（真实且新鲜）
    live_baseline 声称 25/39 特征恒定 → 实际 feat_constant_ratio 仅 0.0625(2/32)
即 A1 据此误删了 7 个**生产实际有真实取值**的特征。

本次修正：
  - 移除 4 维（均经 lm_features 连续采样复核确为恒定）：
      r_dist_atr / sl_mult_used
        推理侧 quality_scorer.py:634 取 snapshot["ai_sl_mult"]，快照无此键 →
        恒 fallback 2.0；且 :593-594 两者取同一值 _mult（注释自称"冗余对齐"），互为副本。
      dev_z_ema200 / entry_atr_ratio
        2026-08-31 追加移除：连续 9 次采样恒定 0.0，推理侧取不到对应数据，
        入模只引入常数维度与噪声。故 37 维 → 35 维。
  - 加回 7 维（A1 误删，推理侧均有真实计算逻辑）：
      dev_z_ema200 / extreme_reversal   quality_scorer.py:591-593 由 enrich_klines 算好
      trend_aligned                     :621-623  close > EMA20 ? 1 : 0
      ds_fake_prob / ds_sl_coeff /
      ds_continuity                     :662-664  读 Redis ai:ds:out:{sym}，实测有票
      entry_atr_ratio                   :643      (entry - close均值)/atr

  - 【2026-09-01 已加回】tmf_* 13 维：TimesFM 抽取已恢复（hcm_ai.timesfm_features
    最新 2026-08-31 19:00 UTC，1924 行；训练产物 features.csv 实测 tmf nonzero≈0.66），
    原"管线停更→恒 0"判断已过时。现加回 MODEL_FEATURE_COLS（35→48 维），
    训练+推理契约同步。

【方法论教训（务必记住）】
  判断"某特征在生产是否真有信息"，判据应是**分位数有无跨度 / 方差是否为零**，
  而不是"中位数是否等于 0"，更不能依赖 live_baseline 这类可能失真的派生统计。
  权威做法：直接读 sidecar 发布的 `lm_features`（真实入模值）交叉验证。

回滚锚点：
  _scratch/_model_feature_cols.py.bak_20260831_a1      (53 维，A1 前)
  _scratch/_model_feature_cols.py.bak_20260831_32dim   (32 维，A1 后、本次修正前)
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
    "macd_slope3",
    "body_wick_ratio",
    "extreme_reversal",
    "di_ratio",
    "di_net",
    "close_mom_atr",
    "trend_aligned",
    # 时段哑变量：分位数 [0,0,0,0,0,0,1,1,1]（约 30% 时间取 1），属有效特征，保留。
    "session_eu",
    "session_us",
    # 外部因子：生产确有真实取值，保留（与 ds_* 是两套体系）。
    "event_proximity_min",
    "macro_risk_score",
    "sentiment_risk_score",
    # DeepSeek 票：实测 ai:ds:out:XAUUSD 有真实值(fake_prob/ai_sl_coeff/continuity)，保留。
    "ds_fake_prob",
    "ds_sl_coeff",
    "ds_continuity",
    # 注：dev_z_ema200 / entry_atr_ratio 已于 2026-08-31 移除——
    # 经 lm_features 连续采样复核，两者在生产恒为 0.0（推理侧取不到对应数据），
    # 入模只会引入常数维度与噪声。
    # ── 【2026-09-01 加回】TimesFM 离线特征 13 维（管线已恢复，非恒 0）──
    # 训练侧 quality_features.load_tmf_features 与推理侧 quality_scorer._load_tmf_for_bar
    # 均按 (symbol, M5 bar_time) 精确 join hcm_ai.timesfm_features（tmf_version=tfm25_pca_v1_sig），
    # 缺省全 0.0 保证训练-推理同分布。PCA 8 维 + 5 派生特征。
    "tmf_pc00", "tmf_pc01", "tmf_pc02", "tmf_pc03", "tmf_pc04", "tmf_pc05", "tmf_pc06", "tmf_pc07",
    "tmf_trend_cont", "tmf_rev_prob", "tmf_vol_cycle", "tmf_mtf_resonance", "tmf_hist_sim",
    # ── 【2026-09-02 新增】verdict（多周期 MTF 加权共识分 ∈[-1,1]，hexp 实时产出）──
    # dir_head 长期缺"多周期共振方向"这一最强方向信号（见对话复盘：dir_hit 仅 0.52、系统性偏多
    # 的根因之一）。verdict 由 hexp_engine 用 M30/H1/H4/D1 按 weight_* 加权算出，含趋势/震荡反向
    # 共识，正是最该喂给方向头的新鲜信息。
    # 训练侧：quality_features._compute_verdict 从多周期 K 线按截至 signal 时刻的已收盘 bar 重算
    #         （与 hexp 同构近似，无迟滞状态机连续性、无未来泄露，HEXP+live_override 全覆盖）。
    # 推理侧：quality_scorer.build_features 直接读 hexp 实时 snap["verdict"]（同源于引擎，最准）。
    # 两侧分布同构。注意：本列必须出现在 MODEL_FEATURE_COLS 且不能被 train 的 drop_cols 丢弃。
    "verdict",
    # ── 【2026-09-02 新增】h1_trend_dir（H1 主趋势方向 ±1/0）──
    # 与 verdict 互补但口径不同：verdict 是多周期加权连续共识分，h1_trend_dir 是**单 H1**
    # 周期明确趋势态(TREND_UP=+1 / TREND_DOWN=-1 / RANGE·TRANSITION·缺失=0，_period_trend_state
    # 口径)。方向头靠它区分"H1 上升趋势中的回调(标签被 dir_trend_align 压 FLAT)"与
    # "震荡顶反转(标签保留 SELL)"——否则模型无 H1 方向输入，趋势对齐标签无法被学习，
    # 超买处仍系统性判 SELL(实证 v79 探针)。绝不能被 train 的 drop_cols 丢弃。
    # 训练侧: quality_features 从 H1 K线 h1_trend_dir_at 重算(已收盘棒,无泄露)。
    # 推理侧: quality_scorer._h1_features 从 PG H1 K线同函数重算(60s 缓存)。
    "h1_trend_dir",
]

# 【TimesFM 特征 2026-08-30】独立导出，供 quality_features / quality_scorer 在 join / 注入时
# 按名遍历，避免与 MODEL_FEATURE_COLS 全表耦合。
#
# 【2026-09-01 已加回】tmf_* 13 维已纳入 MODEL_FEATURE_COLS（35→48 维）：TimesFM 抽取已恢复
# （hcm_ai.timesfm_features 最新 2026-08-31 19:00 UTC），训练/推理同表同口径 join 注入、非恒 0。
# 本列表仍保留独立导出，供 join/注入时按名遍历使用。
TMF_FEATURE_COLS = [
    "tmf_pc00", "tmf_pc01", "tmf_pc02", "tmf_pc03", "tmf_pc04", "tmf_pc05", "tmf_pc06", "tmf_pc07",
    "tmf_trend_cont", "tmf_rev_prob", "tmf_vol_cycle", "tmf_mtf_resonance", "tmf_hist_sim",
]
