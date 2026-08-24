-- ═══════════════════════════════════════════════════════════════
-- 0006_hexp_config.sql
-- 和乘幂(hexp)独立信号源配置键种子 — 《和乘幂信号策略开发文档》
-- 适用：已通过 init.sql 初始化的现有库（增量迁移，向上兼容）
-- 执行时机：需用户授权后手动执行（写 PG 属生产改动红线）
-- 键集合与 signal_tower/hexp_engine.py _DEFAULTS、web/api/hexp.py HEXP_KEYS 完全对齐
-- ═══════════════════════════════════════════════════════════════

BEGIN;

-- ============================================================
-- 和乘幂 — G0 总开关与周期（紫 #8E44AD）
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('hexp.enabled',         'hexp', 'core', 'true',          'bool',   '和乘幂总开关', '关闭后和乘幂不再生产信号（无需切回其他模型）', 'switch', 10, 'global'),
  ('hexp.periods',         'hexp', 'core', 'M5,M30,H1,H4,D1',   'string', '多周期组合', '逗号分隔；按分钟升序，最小=主执行周期(M5)，其余=方向层（含 M30）', 'text', 11, 'global'),
  ('hexp.period_minutes',  'hexp', 'core', 'M1=1,M5=5,M15=15,M30=30,H1=60,H2=120,H4=240,D1=1440', 'string', '周期→分钟映射', '用于排序主/方向层与共振权重归一', 'text', 12, 'global'),
  ('hexp.primary_period',  'hexp', 'core', 'M5',            'string', '主执行周期', '量化交易主周期（入场/止损/震荡策略主执行）', 'text', 13, 'global'),
  ('hexp.direction_min_score', 'hexp', 'core', '0.20',      'number', '方向裁定门槛', '|buy-sell|<0.01 且 max<此值 → NO_TRADE', 'number', 14, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 和乘幂 — G1 幂指数 k 自适应（核心算法）
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('hexp.k.base',            'hexp', 'k', '1.5',  'number', 'k 基准值', '幂指数基准（中性）；k=1 为普通加权和', 'number', 20, 'global'),
  ('hexp.k.min',             'hexp', 'k', '0.5',  'number', 'k 下限', '凹收敛极限（k<1 要求多因子共识）', 'number', 21, 'global'),
  ('hexp.k.max',             'hexp', 'k', '3.0',  'number', 'k 上限', '凸增强极限（k>1 强因子主导）', 'number', 22, 'global'),
  ('hexp.k.alpha',           'hexp', 'k', '0.8',  'number', 'ADX 影响权重', 'k 自适应公式中 ADX 强度系数', 'number', 23, 'global'),
  ('hexp.k.beta',            'hexp', 'k', '0.4',  'number', 'BBW 影响权重', 'k 自适应公式中带宽分位系数', 'number', 24, 'global'),
  ('hexp.k.state_trend',     'hexp', 'k', '2.0',  'number', '趋势市 k 基准', 'TREND 状态：凸增强，强因子主导', 'number', 25, 'global'),
  ('hexp.k.state_range',     'hexp', 'k', '0.65', 'number', '震荡市 k 基准', 'RANGE 状态：凹收敛，抑制单因子假突破', 'number', 26, 'global'),
  ('hexp.k.state_transition','hexp', 'k', '1.0',  'number', '转换中 k 基准', 'TRANSITION 状态：平衡中性', 'number', 27, 'global'),
  ('hexp.k.state_fade',      'hexp', 'k', '2.5',  'number', '趋势衰竭 k 基准', 'TREND_FADE：强凸，只信最强因子', 'number', 28, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 和乘幂 — G2 因子权重（运行时归一）
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('hexp.factor.adx_weight',   'hexp', 'factor', '25', 'number', 'ADX 权重', '趋势强度因子（方向无关，DI 定方向）', 'number', 30, 'global'),
  ('hexp.factor.er_weight',    'hexp', 'factor', '25', 'number', 'ER 权重', 'Kaufman 效率比（直线程度，抗震荡欺骗）', 'number', 31, 'global'),
  ('hexp.factor.ma_weight',    'hexp', 'factor', '20', 'number', 'MA 权重', 'EMA20/50/100 三腿排列 + EMA20 回归斜率', 'number', 32, 'global'),
  ('hexp.factor.bbw_weight',   'hexp', 'factor', '15', 'number', 'BBW 权重', '布林带宽 120 根分位数（挤压→扩张）', 'number', 33, 'global'),
  ('hexp.factor.hurst_weight', 'hexp', 'factor', '10', 'number', 'Hurst 权重', 'R/S 长期记忆（>0.5 趋势持续 / <0.5 均值回归）', 'number', 34, 'global'),
  ('hexp.factor.rsi_weight',   'hexp', 'factor', '5',  'number', 'RSI 权重', '超买超卖辅助（趋势/震荡双模式）', 'number', 35, 'global'),
  ('hexp.factor.mm_weight',    'hexp', 'factor', '15', 'number', '微结构动量权重', 'M1 幂律加权动量（前置预警，第 7 因子）', 'number', 36, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 和乘幂 — G3 因子参数
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('hexp.adx.min',          'hexp', 'factor_param', '15',   'number', 'ADX 归一下界', 'ADX≤此值归一为 0（无趋势）', 'number', 40, 'global'),
  ('hexp.adx.max',          'hexp', 'factor_param', '35',   'number', 'ADX 归一上界', 'ADX≥此值归一为 1（强趋势）', 'number', 41, 'global'),
  ('hexp.er.period',        'hexp', 'factor_param', '20',   'int',    'ER 周期', 'Kaufman 效率比回看窗口（bars）', 'number', 42, 'global'),
  ('hexp.er.min',           'hexp', 'factor_param', '0.10', 'number', 'ER 归一下界', 'ER≤此值归一为 0（震荡）', 'number', 43, 'global'),
  ('hexp.er.max',           'hexp', 'factor_param', '0.40', 'number', 'ER 归一上界', 'ER≥此值归一为 1（极强单边）', 'number', 44, 'global'),
  ('hexp.ma.ema_fast',      'hexp', 'factor_param', '20',   'int',    'EMA 短周期', 'EMA 排列最短腿', 'number', 45, 'global'),
  ('hexp.ma.ema_mid',       'hexp', 'factor_param', '50',   'int',    'EMA 中周期', 'EMA 排列中腿', 'number', 46, 'global'),
  ('hexp.ma.ema_long',      'hexp', 'factor_param', '100',  'int',    'EMA 长周期', 'EMA 排列最长腿', 'number', 47, 'global'),
  ('hexp.ma.align_score',   'hexp', 'factor_param', '50',   'number', '排列对齐分', 'EMA 三腿全排列加/减分（±）', 'number', 48, 'global'),
  ('hexp.ma.slope_norm_bp', 'hexp', 'factor_param', '3.0',  'number', 'EMA20 斜率基准(bp)', '斜率绝对值÷此值→斜率分（1bp=0.01%）', 'number', 49, 'global'),
  ('hexp.ma.slope_bars',    'hexp', 'factor_param', '10',   'int',    '斜率回归根数', 'EMA20 线性回归窗口', 'number', 50, 'global'),
  ('hexp.bbw.window',       'hexp', 'factor_param', '120',  'int',    'BBW 分位窗口', '当前 BBW 在最近 N 根中的分位', 'number', 51, 'global'),
  ('hexp.bbw.boll_period',  'hexp', 'factor_param', '20',   'int',    'BBW 布林周期', '带宽计算所用布林周期', 'number', 52, 'global'),
  ('hexp.bbw.boll_std',     'hexp', 'factor_param', '2.0',  'number', 'BBW 布林标准差', '带宽计算标准差倍数', 'number', 53, 'global'),
  ('hexp.hurst.min',        'hexp', 'factor_param', '0.40', 'number', 'Hurst 归一下界', '<0.5 偏均值回归', 'number', 54, 'global'),
  ('hexp.hurst.max',        'hexp', 'factor_param', '0.60', 'number', 'Hurst 归一上界', '>0.5 偏持久趋势', 'number', 55, 'global'),
  ('hexp.hurst.max_lag',    'hexp', 'factor_param', '32',   'int',    'Hurst 滞后窗', 'R/S 估计最大滞后（bars）', 'number', 56, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 和乘幂 — G4 状态机（迟滞）+ 微结构动量
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('hexp.state.enter_score',  'hexp', 'state', '60', 'number', '进入趋势门槛', 'TrendScore≥此值且方向明确→进入趋势态（迟滞上沿）', 'number', 60, 'global'),
  ('hexp.state.exit_score',   'hexp', 'state', '40', 'number', '退出趋势门槛', 'TrendScore<此值→退出趋势（迟滞下沿）', 'number', 61, 'global'),
  ('hexp.state.confirm_bars', 'hexp', 'state', '1',  'int',    '确认根数', '状态切换需连续确认的 bar 数（防抖）', 'number', 62, 'global'),
  ('hexp.mm.alpha',           'hexp', 'mm', '0.5',   'number', '幂律衰减指数 α', '越大衰减越快越敏捷（0.5 平滑 / 1.0 平衡 / 2.0 敏捷）', 'number', 63, 'global'),
  ('hexp.mm.window',          'hexp', 'mm', '20',    'int',    '动量回看窗口', 'M1 级回看根数', 'number', 64, 'global'),
  ('hexp.mm.period',          'hexp', 'mm', 'M1',    'string', '微结构数据周期', '幂律动量所用 K 线周期（默认 M1）', 'text', 65, 'global'),
  ('hexp.mm.scale',           'hexp', 'mm', '0.002', 'number', 'MM 归一尺度', 'tanh 归一分母（对数收益量级）', 'number', 66, 'global'),
  ('hexp.mm.accel_k_boost',   'hexp', 'mm', '0.3',   'number', '共振加速 k 增量', 'MM 与大因子共振时 k 临时提升量', 'number', 67, 'global'),
  ('hexp.mm.accel_threshold', 'hexp', 'mm', '0.7',   'number', '共振加速阈值', '|f_mm| 超此值且大因子>0.5 才触发加速', 'number', 68, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 和乘幂 — G5 共振矩阵（方向裁决；主执行周期权重=0 不自我裁决）
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('hexp.mtf.weight_D1',        'hexp', 'mtf', '0.25', 'number', '共振权重 D1', '日线方向裁决权重', 'number', 70, 'global'),
  ('hexp.mtf.weight_H4',        'hexp', 'mtf', '0.35', 'number', '共振权重 H4', '4 小时方向裁决权重（主方向层）', 'number', 71, 'global'),
  ('hexp.mtf.weight_H1',        'hexp', 'mtf', '0.25', 'number', '共振权重 H1', '1 小时方向裁决权重', 'number', 72, 'global'),
  ('hexp.mtf.weight_M30',       'hexp', 'mtf', '0.15', 'number', '共振权重 M30', '30 分钟方向裁决权重（选配）', 'number', 73, 'global'),
  ('hexp.mtf.long_threshold',   'hexp', 'mtf', '0.5',  'number', '只做多阈值', 'verdict≥此值→禁止开空（铁律）', 'number', 74, 'global'),
  ('hexp.mtf.short_threshold',  'hexp', 'mtf', '-0.5', 'number', '只做空阈值', 'verdict≤此值→禁止开多（铁律）', 'number', 75, 'global'),
  ('hexp.resonance.bonus',      'hexp', 'mtf', '0.10', 'number', '共振加成系数', 'verdict 与信号同向时 hp ×(1+|verdict|×此值)', 'number', 76, 'global'),
  ('hexp.resonance.penalty',    'hexp', 'mtf', '0.15', 'number', '共振惩罚系数', 'verdict 与信号反向时 hp ×(1-|verdict|×此值)', 'number', 77, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 和乘幂 — G6 评分卡（S/A/B/C/红灯分级）
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('hexp.scorecard.weight_resonance', 'hexp', 'scorecard', '25', 'number', '评分-共振权重', '多周期共振维权重', 'number', 80, 'global'),
  ('hexp.scorecard.weight_state',     'hexp', 'scorecard', '20', 'number', '评分-状态权重', '和乘幂强度维权重（hp_100）', 'number', 81, 'global'),
  ('hexp.scorecard.weight_entry',     'hexp', 'scorecard', '20', 'number', '评分-入场权重', '入场技术维（贴 EMA20 回踩位 + 实体占比）', 'number', 82, 'global'),
  ('hexp.scorecard.weight_position',  'hexp', 'scorecard', '15', 'number', '评分-位置权重', '距 Donchian(20) 反向边界空间', 'number', 83, 'global'),
  ('hexp.scorecard.weight_vol',       'hexp', 'scorecard', '10', 'number', '评分-波动权重', 'ATR 分位 30-70% 最佳', 'number', 84, 'global'),
  ('hexp.scorecard.weight_session',   'hexp', 'scorecard', '10', 'number', '评分-时段权重', '伦敦/纽约重叠满分，亚盘减半', 'number', 85, 'global'),
  ('hexp.scorecard.pass_threshold',   'hexp', 'scorecard', '45', 'number', '放行门槛', '总分<此值 → 红灯禁止开仓', 'number', 86, 'global'),
  ('hexp.scorecard.b_threshold',      'hexp', 'scorecard', '60', 'number', 'B 级门槛', '总分≥此值评 B 级', 'number', 87, 'global'),
  ('hexp.scorecard.a_threshold',      'hexp', 'scorecard', '75', 'number', 'A/S 级门槛', '总分≥此值评 A 级（再叠加 hp 条件升 S）', 'number', 88, 'global'),
  ('hexp.scorecard.s_hp_min',         'hexp', 'scorecard', '60', 'number', 'S 级 hp 下限', '升 S 所需的和乘幂强度下限', 'number', 89, 'global'),
  ('hexp.scorecard.hp_floor',         'hexp', 'scorecard', '30', 'number', 'hp 地板', 'hp_100 低于此值一律红灯', 'number', 90, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 和乘幂 — G7 执行参数（桥消费 ai_sl_mult / ai_tp_mult / lot）
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('hexp.exec.sl_atr_mult',         'hexp', 'exec', '2.0', 'number', 'SL ATR 倍数', '止损距离 = 此倍数 × ATR', 'number', 91, 'global'),
  ('hexp.exec.rr_min',              'hexp', 'exec', '1.5', 'number', '最低盈亏比 R:R', 'TP = SL 距离 × 此值', 'number', 92, 'global'),
  ('hexp.exec.lot_mult',            'hexp', 'exec', '1.0', 'number', '仓位倍数', '下单手数倍率（再乘分级系数）', 'number', 93, 'global'),
  ('hexp.exec.grade_lot_s',         'hexp', 'exec', '1.2', 'number', 'S 级仓位系数', 'S 级信号手数乘数', 'number', 94, 'global'),
  ('hexp.exec.grade_lot_a',         'hexp', 'exec', '1.0', 'number', 'A 级仓位系数', 'A 级信号手数乘数', 'number', 95, 'global'),
  ('hexp.exec.grade_lot_b',         'hexp', 'exec', '0.5', 'number', 'B 级仓位系数', 'B 级信号手数乘数', 'number', 96, 'global'),
  ('hexp.exec.grade_lot_c',         'hexp', 'exec', '0.5', 'number', 'C 级仓位系数', 'C 级信号手数乘数', 'number', 97, 'global'),
  ('hexp.exec.transition_lot_mult', 'hexp', 'exec', '0.5', 'number', '转换态降仓系数', '任一周期 TRANSITION 时仓位再乘此值', 'number', 98, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 和乘幂 — Prompt 模板（多模型提示词路由兼容；hexp 不用 AI 亦需占位键）
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('signal_tower.prompt.hexp.system_prompt', 'signal_tower', 'prompt', '和乘幂(hexp)为纯本地规则模型，不走 LLM 推理。', 'string', '和乘幂系统提示词', '占位键：hexp 不调用 AI，仅为多模型 prompt 路由兼容', 'textarea', 10, 'global'),
  ('signal_tower.prompt.hexp.user_prompt_template', 'signal_tower', 'prompt', '和乘幂(hexp)为纯本地规则模型，不走 LLM 推理。', 'string', '和乘幂用户提示词模板', '占位键：hexp 不调用 AI，仅为多模型 prompt 路由兼容', 'textarea', 11, 'global')
ON CONFLICT (config_key) DO NOTHING;

COMMIT;

-- 验证：SELECT count(*) FROM hcm_config.metadata WHERE config_key LIKE 'hexp.%';  -- 期望 65
