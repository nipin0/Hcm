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
    #
    # ──【2026-09-10 下线 event_proximity_min】已从质量头契约移除（37 → 36 维）。──
    # 该特征【非恒定、确有跨度】(p0=558 → p100=26212 分钟，unique=1488)，但它是
    # 【时间代理 + 非平稳】，而不是可用预测因子：
    #   (1) corr(feature, created_at) = -0.9373 —— 几乎就是时间索引本身
    #   (2) 单特征模型 test AUC = 0.4992（0.5 = 随机）→ 自身零信息
    #   (3) 时序切分下 train[1350, 26212] 中位 13643 / test[558, 8655] 中位 4604，
    #       **重叠仅 28.5%** → 大量测试值低于训练集下界，树模型被迫外推
    # 根因：hcm_market.event_calendar 全库仅 17 条(is_active AND importance>=2)，
    #   样本期恰跨 08-15→09-04 的 20 天空档，"距下一事件分钟数"在空档内随日历线性
    #   递减 → 退化为时间轴。
    #   注：这【不是】未来泄露（事件日历提前公布、口径合法），而是"日历太稀疏
    #   → 特征非平稳"，与 tmf_* 的"退化常数"是两种不同病，勿混为一谈。
    #
    # ⚠️【诚实修正：性能证据仅为【中性】，不是"有害"】
    #   2026-09-10 复核（n=1488，时序 TimeSeriesSplit，5 种子 × 5 折 = 25 组配对）：
    #     含该特征 37 维 AUC = 0.6698 ± 0.1100
    #     去掉后   36 维 AUC = 0.6655 ± 0.1517
    #     ΔAUC = -0.0043，SE = 0.0135，95%CI = [-0.0308, +0.0222]（跨 0，无显著差异）
    #   排列重要性：打乱该列 ΔAUC = -0.0030（同样跨 0，点估计略偏"有用"）。
    #   → 即【既无显著收益、也无显著损害】。此前一度记录的"打乱后 AUC 升 0.0211"
    #     出自另一套评估口径（基线 AUC 0.8229），在本时序配对协议下【未能复现】。
    #   故本项下线的理由是【稳健性】：时间代理 + 71.5% 分布不重叠 + 不可外推 +
    #     历史上曾致 27h 纯 HEXP 降级（现仍需 PSI 豁免）。
    #   预期性能影响 ≈ 0；收益是消除一个不可外推的输入与运维噪声源。
    # 回滚：把 "event_proximity_min" 加回本列表即可（推理侧 build_features 仍会产出
    #   该列，score_one 按 model.feature_name() 取列，故旧 37 维模型不受影响）。
    "macro_risk_score",
    "sentiment_risk_score",
    # DeepSeek 票：实测 ai:ds:out:XAUUSD 有真实值(fake_prob/ai_sl_coeff/continuity)，保留。
    "ds_fake_prob",
    "ds_sl_coeff",
    "ds_continuity",
    # 注：dev_z_ema200 / entry_atr_ratio 已于 2026-08-31 移除——
    # 经 lm_features 连续采样复核，两者在生产恒为 0.0（推理侧取不到对应数据），
    # 入模只会引入常数维度与噪声。
    # ──【2026-09-10 下线 TimesFM 特征】13 维 tmf_* 已从质量头契约移除。──
    # 依据：AUC 消融实测（_scratch/_tmp_ablation.py，features.csv/labels.csv 2026-09-09）：
    #   含 tmf   test_AUC = 0.7371
    #   不含 tmf test_AUC = 0.7575
    #   增量 ΔAUC = -0.0205（TimesFM 特征不仅无增益，反而拉低 AUC）；
    #   且 Top5 特征重要性无一 tmf_*，tmf_hist_sim 实测恒 ≈0.9994（退化常数）。
    # 根因：TimesFM 2.5 单变量(仅 close) + 通用低频序列预训练 + normalize_inputs=False
    #   → PC1 独占 99.95% 方差（embedding 退化为标量）→ 任务失配 + 特征退化。
    # TMF_FEATURE_COLS 仍保留（仅供离线审计/追溯），但不再入 MODEL_FEATURE_COLS。
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
    # 【2026-09-10】不确定度特征（timesfm_features.uncertainty_features 从 9 分位数 q 提取）
    #
    # 【2026-09-10 消融结论：不提升，维持不入模】
    # 实测（消融脚本；features.csv/labels.csv 2026-09-09，n=1470，
    #       时序 TimeSeriesSplit 5 折 × 5 种子 = 25 组【配对】观测，两模型同折同种子）：
    #   基线 37 维       AUC = 0.6683 ± 0.1419
    #   +不确定度 41 维  AUC = 0.6549 ± 0.1332
    #   ΔAUC = -0.0134，SE = 0.0102，95%CI = [-0.0335, +0.0067]（跨 0，无显著差异）
    #   不确定度仅胜出 5/25 组 —— 幅度虽不显著，但方向一致偏负。
    #
    # ⚠️ 本次根因【不同于】上方旧 13 维的下线原因，勿混为一谈：
    #   旧 13 维：embedding 退化（PC1 独占 99.95% 方差 → 退化为标量，tmf_pc00 实测
    #             span=0.0038 / std=0.0005），属"特征本身没信息"。
    #   新 qf_*  ：【并非】退化常数，分位数均有真实跨度
    #             （width span=19.40/std=2.57、skew 0.72/0.087、
    #               uptail 8.27/1.09、growth 0.55/0.059）。
    #             即"信息真实存在，但不预测信号质量"；且模型确实在用
    #             （tmf_qf_skew 重要性排名第 2，占 5.74% ≈ 均摊 2.44% 的 2.4 倍），
    #             说明是【样本内可拟合、样本外不泛化】，1470 样本再加 4 维只增过拟合。
    # 决策：保留本列表供审计/追溯，【不】加入 MODEL_FEATURE_COLS。
    "tmf_qf_width", "tmf_qf_skew", "tmf_qf_uptail", "tmf_qf_growth",
]
