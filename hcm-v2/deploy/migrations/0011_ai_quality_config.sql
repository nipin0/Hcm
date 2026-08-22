-- ═══════════════════════════════════════════════════════════════
-- 0011_ai_quality_config.sql
-- AI 独立模块（LightGBM 信号质量评分器 + DeepSeek 异步 + 耦合闸门 + 持仓调仓）配置种子
-- 适用：已通过 init.sql 初始化的现有库（增量迁移，向上兼容）
-- 执行时机：需用户授权后手动执行（写 PG 属生产改动红线）
--
-- 设计约束（用户硬性要求）：
--   · 所有变量参数零硬编码——全量进 hcm_config.metadata（PG SoT + Redis 双写）。
--   · 默认全关（ai.enabled=false）→ 纯 HEXP 运行，AI 零介入；冷启动/模型缺失自动降级。
--   · 纪律红线：AI 只有否决权 + 降/升级权，无独立开仓权（方向永远由 HEXP 决定）。
--   · DeepSeek 仅后台异步数据源；失败/超时/限流自动降级纯 HEXP。
-- ═══════════════════════════════════════════════════════════════

BEGIN;

-- ============================================================
-- AI 模块 · 总开关
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('ai.enabled', 'ai_quality', 'top', 'false', 'bool', 'AI 质量模块总开关', 'false=纯 HEXP 运行，AI 零介入；true=启用 AI 质量过滤/耦合', 'switch', 1, 'global'),
  ('ai.mode',    'ai_quality', 'top', 'decoupled', 'string', 'AI 耦合模式', 'decoupled=纯 HEXP 分数触发；coupled=AI 综合评分触发', 'select', 2, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- AI · LightGBM 本地评分器（AI_LM）
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('ai.lm.enabled',              'ai_quality', 'lm', 'false', 'bool',   'LightGBM 评分器开关', 'true=对每个 HEXP 信号打分并过滤/升降级', 'switch', 10, 'global'),
  ('ai.lm.model_path',           'ai_quality', 'lm', '',     'string', '模型文件路径', '空或加载失败→自动降级纯 HEXP（冷启动安全）', 'text', 11, 'global'),
  ('ai.lm.calib_path',           'ai_quality', 'lm', '',     'string', '概率校准器路径', 'Platt/isotonic 校准器（pkl），空=不校准', 'text', 12, 'global'),
  ('ai.lm.model_version',        'ai_quality', 'lm', 'v0',   'string', '模型版本', '回滚=指向旧版本文件', 'text', 13, 'global'),
  ('ai.lm.pass_threshold',       'ai_quality', 'lm', '0.50', 'number', '否决阈值(保留兼容)', '历史键，现由 veto_floor 取代否决语义；p < pass_threshold → DOWNGRADE', 'number', 14, 'global'),
  ('ai.lm.veto_floor',           'ai_quality', 'lm', '0.30', 'number', '否决地板阈值', 'c_ai/100 < 此值才否决（极低分）；否则 HOLD/UPGRADE 放行。避免中等分(如38)被全杀', 'number', 14, 'global'),
  ('ai.lm.down_threshold',       'ai_quality', 'lm', '0.60', 'number', '降级阈值', 'pass ≤ p < 此值 → 降一级（C→红灯, B→C）', 'number', 15, 'global'),
  ('ai.lm.up_threshold',         'ai_quality', 'lm', '0.70', 'number', '升级阈值', 'p ≥ 此值 → 升一级（B→A, A→S）', 'number', 16, 'global'),
  ('ai.lm.veto_quantile',        'ai_quality', 'lm', '0.50', 'number', '否决分位（重锚）', 'p 低于此分位 → 过滤（相对基率，替代绝对阈值，推荐）', 'number', 17, 'global'),
  ('ai.lm.down_quantile',        'ai_quality', 'lm', '0.70', 'number', '降级分位（重锚）', 'p 低于此分位 → 降一级', 'number', 18, 'global'),
  ('ai.lm.up_quantile',          'ai_quality', 'lm', '0.85', 'number', '升级分位（重锚）', 'p 高于此分位 → 升一级', 'number', 19, 'global'),
  ('ai.lm.min_samples_train',    'ai_quality', 'lm', '500',  'int',    '最少训练样本', '标注样本 < 此值不出模型，保持纯 HEXP', 'number', 20, 'global'),
  ('ai.lm.retrain_cron',         'ai_quality', 'lm', '0 2 * * 1', 'string', '重训 cron', '离线重训调度（默认每周一 02:00）', 'text', 21, 'global'),
  ('ai.lm.label_r_win',          'ai_quality', 'lm', '1.0',  'number', '标签止盈 R 倍数', '先触 +此×R 记 win=1（重锚：对称 1R）', 'number', 22, 'global'),
  ('ai.lm.label_r_loss',         'ai_quality', 'lm', '1.0',  'number', '标签止损 R 倍数', '先触 -此×R 记 loss=0', 'number', 23, 'global'),
  ('ai.lm.label_horizon_bars',   'ai_quality', 'lm', '12',   'int',    '标签回看 M5 根数', '入场后此根数内判定先触', 'number', 24, 'global'),
  ('ai.lm.label_sl_atr_fallback','ai_quality', 'lm', '2.0',  'number', 'SL 推导回退 ATR 倍数', 'sl_price 未落库且无 ai_sl_mult 时，R = 此值 × ATR', 'number', 25, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- AI · DeepSeek 异步数据源（AI_DS）
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('ai.ds.enabled',              'ai_quality', 'ds', 'false', 'bool',   'DeepSeek 异步数据源开关', 'true=后台异步刷新 3 输出（真假概率/sl_coeff/continuity）', 'switch', 30, 'global'),
  ('ai.ds.timeout_sec',          'ai_quality', 'ds', '15',   'number', '调用超时（秒）', '超时即降级（不阻塞实时决策）', 'number', 31, 'global'),
  ('ai.ds.cache_ttl_min',        'ai_quality', 'ds', '30',   'int',    '输出缓存 TTL（分）', '3 输出在 Redis 缓存的有效期', 'number', 32, 'global'),
  ('ai.ds.sl_coeff_min',         'ai_quality', 'ds', '0.8',  'number', 'ai_sl_coeff 下限', '自适应止损系数硬下界（ATR 倍数）', 'number', 33, 'global'),
  ('ai.ds.sl_coeff_max',         'ai_quality', 'ds', '1.5',  'number', 'ai_sl_coeff 上限', '自适应止损系数硬上界（ATR 倍数）', 'number', 34, 'global'),
  ('ai.ds.fallback_sl_coeff',    'ai_quality', 'ds', '0',    'number', '止损回退系数', '0=回退 hexp.exec.sl_atr_mult；>0=固定回退系数', 'number', 35, 'global'),
  ('ai.ds.fallback_continuity',  'ai_quality', 'ds', '50',   'int',    '延续分回退值', 'DeepSeek 不可用时 continuity_score=此值（中性）', 'number', 36, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- AI · 融合权重（AI_FUSE）：LightGBM + DeepSeek 校准 → c_ai
-- 闭环核心：sidecar 的 LightGBM 票(ai_score) 与 DeepSeek 异步票(fake_prob)
-- 经 calibrate_lm_score 融合成单一 c_ai(0-100)，再交 quality_gate 裁决。
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('ai.fuse.w_lm',           'ai_quality', 'fuse', '0.6',  'number', 'LightGBM 融合权重', '本地快速票权重（0-1）；与 w_ds 归一化', 'number', 37, 'global'),
  ('ai.fuse.w_ds',           'ai_quality', 'fuse', '0.4',  'number', 'DeepSeek 融合权重', '异步语义票权重（0-1）；与 w_lm 归一化', 'number', 38, 'global'),
  ('ai.fuse.ds_max_age_sec', 'ai_quality', 'fuse', '900',  'number', 'DeepSeek 票最大龄（秒）', '超过此龄的旧判断丢弃（行情已走远，不参与实时裁决）', 'number', 39, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- AI · 耦合闸门（AI_CPL）
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('ai.cpl.enabled',     'ai_quality', 'cpl', 'false', 'bool',   '耦合闸门开关', 'true=总分=w(k)·S_hp+(1-w(k))·C_ai 触发下单', 'switch', 50, 'global'),
  ('ai.cpl.w_trend',     'ai_quality', 'cpl', '0.7',   'number', '趋势档 S_hp 权重', 'k > k_trend_min 时 S_hp 权重（C_ai=1-w）', 'number', 51, 'global'),
  ('ai.cpl.w_neutral',   'ai_quality', 'cpl', '0.6',   'number', '中性档 S_hp 权重', 'k_range_max < k ≤ k_trend_min 时', 'number', 52, 'global'),
  ('ai.cpl.w_range',     'ai_quality', 'cpl', '0.5',   'number', '震荡档 S_hp 权重', 'k ≤ k_range_max 时（强 AI 过滤，C_ai 上限 0.5）', 'number', 53, 'global'),
  ('ai.cpl.k_trend_min', 'ai_quality', 'cpl', '1.2',   'number', '趋势 k 下界', 'k > 此值判趋势档', 'number', 54, 'global'),
  ('ai.cpl.k_range_max', 'ai_quality', 'cpl', '0.5',   'number', '震荡 k 上界', 'k ≤ 此值判震荡档', 'number', 55, 'global'),
  ('ai.cpl.tier_high',   'ai_quality', 'cpl', '85',    'number', '高总分档', '总分 > 此值 → 基础手数 × lot_high', 'number', 56, 'global'),
  ('ai.cpl.tier_mid',    'ai_quality', 'cpl', '70',    'number', '中总分档', '总分 > 此值 → 基础手数 × 1', 'number', 57, 'global'),
  ('ai.cpl.tier_low',    'ai_quality', 'cpl', '60',    'number', '低总分档', '总分 > 此值 → 基础手数 × lot_low；≤ 此值不发信号', 'number', 58, 'global'),
  ('ai.cpl.lot_high',    'ai_quality', 'cpl', '1.5',   'number', '高手数倍率', '高总分档手数倍率', 'number', 59, 'global'),
  ('ai.cpl.lot_low',     'ai_quality', 'cpl', '0.5',   'number', '低手数倍率', '低总分档手数倍率', 'number', 60, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- AI · 持仓调仓（AI_CONT，最重，默认关 + log-only）
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('ai.cont.enabled',    'ai_quality', 'cont', 'false', 'bool',   '持仓调仓开关', 'true=用 continuity_score 调仓（默认关，独立 kill-switch）', 'switch', 70, 'global'),
  ('ai.cont.strong_min', 'ai_quality', 'cont', '70',    'int',    '强延续下界', 'continuity ≥ 此值 → 放宽止盈/追踪/允许加仓复核', 'number', 71, 'global'),
  ('ai.cont.weak_max',   'ai_quality', 'cont', '49',    'int',    '弱延续上界', 'continuity < 此值 → 收紧止损/压缩止盈/锁利', 'number', 72, 'global'),
  ('ai.cont.mode',       'ai_quality', 'cont', 'log',   'string', '调仓执行模式', 'log=仅记录；act=真实执行（红线，另行确认）', 'select', 73, 'global')
ON CONFLICT (config_key) DO NOTHING;

COMMIT;
