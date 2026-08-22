-- ═══════════════════════════════════════════════════════════════
-- 0003_co_source_config.sql
-- 共源信号增强方案（hcm-co-source-model-prd.md v1.1）配置与数据表
-- 适用：已通过 init.sql 初始化的现有库（增量迁移，向上兼容）
-- 执行时机：需用户授权后手动执行（写 PG 属生产改动红线）
-- ═══════════════════════════════════════════════════════════════
--
-- ⚠️ 键数量说明（重要，交付时同步用户）：
--   PRD §5.2 标注「55 个 co.* 键」，但 §4.5.3 字段表实际展开后更多，
--   且为满足硬约束 ①「禁用硬编码」，补入了 4 个 PRD 表缺失但逻辑必需的键：
--     · co.filter.f5_penalty      (PRD F5 描述"-30分"却无键 → 补)
--     · co.gate.score_scale       (PRD 门槛/扣分值为 0-100，引擎 pre_score 为 0-1，需归一)
--     · co.gate.adx_strong        (PRD 强趋势"ADX>35"阈值无键 → 补)
--     · co.gate.shock.atr_mult    (PRD 突发波动"ATR翻倍"无基线键 → 补)
--   故本迁移共 60 个键（其余均严格对齐 PRD 字面默认值）。
-- ═══════════════════════════════════════════════════════════════

BEGIN;

-- ============================================================
-- 共源信号 — G1 校准因子（蓝 #3b82f6）
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('co.calib.pre_trend', 'co_source', 'calib', '1.0', 'number', '前趋势校准因子', 'PRE_TREND 体制时 raw_score 乘以此系数（0.5-1.5）', 'number', 10, 'global'),
  ('co.calib.trend',     'co_source', 'calib', '1.0', 'number', '强趋势校准因子', 'TREND 体制(ADX>35)时乘数；>1=更积极开仓', 'number', 11, 'global'),
  ('co.calib.trend_fade','co_source', 'calib', '1.0', 'number', '趋势衰减校准因子', 'TREND_FADE 体制（趋势转弱）时乘数', 'number', 12, 'global'),
  ('co.calib.range',     'co_source', 'calib', '1.0', 'number', '震荡校准因子', 'RANGE 体制(ADX<20)时乘数（通常<1 抑制开仓）', 'number', 13, 'global'),
  ('co.calib.neutral',   'co_source', 'calib', '1.0', 'number', '中性校准因子', 'NEUTRAL 体制时乘数', 'number', 14, 'global'),
  ('co.calib.min_days',  'co_source', 'calib', '7',   'int',    '校准激活最少天数', '标注数据 ≥ 此天数后才启用校准因子（冷启动期恒=1.0）', 'number', 15, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 共源信号 — G2 假信号过滤（橙 #f59e0b）
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('co.filter.f1_enabled',     'co_source', 'filter', 'true',  'bool',   'F1 周期背离检测', 'H1 价格新高/新低但 MACD/RSI 背离时扣分', 'switch', 20, 'global'),
  ('co.filter.f1_penalty',     'co_source', 'filter', '20',    'number', 'F1 扣分值', '背离命中后的扣分（0-100，经 score_scale 归一到 0-1）', 'number', 21, 'global'),
  ('co.filter.f2_enabled',     'co_source', 'filter', 'true',  'bool',   'F2 布林收口假突破', '带宽 < 近20根均值比例 时作废信号', 'switch', 22, 'global'),
  ('co.filter.f2_ratio',       'co_source', 'filter', '50',    'number', 'F2 带宽比例阈值', '当前带宽 / 近20根均值 < 此% 即触发（10-80）', 'number', 23, 'global'),
  ('co.filter.f3_enabled',     'co_source', 'filter', 'true',  'bool',   'F3 数据窗口期降分', '重大数据公布前降分', 'switch', 24, 'global'),
  ('co.filter.f3_minutes',     'co_source', 'filter', '30',    'int',    'F3 窗口时间(分钟)', '数据公布前几分钟生效（10-60）', 'number', 25, 'global'),
  ('co.filter.f3_penalty',     'co_source', 'filter', '25',    'number', 'F3 降分值', '数据窗口期扣分（0-100）', 'number', 26, 'global'),
  ('co.filter.f4_enabled',     'co_source', 'filter', 'true',  'bool',   'F4 超买超卖钝化', '单边行情 RSI 持续极端时取消反向信号', 'switch', 27, 'global'),
  ('co.filter.f4_rsi_upper',   'co_source', 'filter', '70',    'number', 'F4 RSI 超买阈值', 'RSI 持续高于此值且方向=SELL 时取消做空（60-80）', 'number', 28, 'global'),
  ('co.filter.f4_rsi_lower',   'co_source', 'filter', '30',    'number', 'F4 RSI 超卖阈值', 'RSI 持续低于此值且方向=BUY 时取消做多（20-40）', 'number', 29, 'global'),
  ('co.filter.f5_enabled',     'co_source', 'filter', 'true',  'bool',   'F5 连续亏损熔断', '连续 N 笔全止损时扣分 + 触发极端补充调用', 'switch', 30, 'global'),
  ('co.filter.f5_consecutive', 'co_source', 'filter', '3',     'int',    'F5 连续亏损次数', '达到此次数即熔断（2-5）', 'number', 31, 'global'),
  ('co.filter.f5_penalty',     'co_source', 'filter', '30',    'number', 'F5 扣分值', '连续亏损熔断扣分（约束①：PRD 描述"-30分"但未给键，此处补；0-100）', 'number', 32, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 共源信号 — G3 自适应门槛（绿 #10b981）
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('co.gate.score_scale',       'co_source', 'gate', '100',  'int',    '分数归一尺度', 'PRD 0-100 门槛/扣分 ÷ 此值 映射到引擎 0-1 尺度（约束①：PRD 缺键，补）', 'number', 40, 'global'),
  ('co.gate.adx_strong',        'co_source', 'gate', '35',   'int',    '强趋势ADX阈值', 'ADX ≥ 此值判为强趋势带（约束①：PRD"ADX>35"缺键，补）', 'number', 41, 'global'),
  ('co.gate.shock.atr_mult',    'co_source', 'gate', '2.0',  'number', '突发波动ATR倍数', 'vol_factor ≥ 此值判为突发波动带（约束①：PRD"ATR翻倍"缺基线键，补）', 'number', 42, 'global'),
  ('co.gate.strong.trend',      'co_source', 'gate', '65',   'number', '强趋势打分门槛', 'ADX>35 时信号放行最低分（÷scale）', 'number', 43, 'global'),
  ('co.gate.strong.lot',        'co_source', 'gate', '1.0',  'number', '强趋势仓位系数', '标准仓位乘数（0.5-2.0）', 'number', 44, 'global'),
  ('co.gate.strong.sl_atr',     'co_source', 'gate', '0.5',  'number', '强趋势SL ATR倍数', '止损 ATR 倍数（0.2-1.0）', 'number', 45, 'global'),
  ('co.gate.strong.rr_min',     'co_source', 'gate', '2.0',  'number', '强趋势最低盈亏比', '最低风险回报比（1.0-3.0）', 'number', 46, 'global'),
  ('co.gate.weak.trend',        'co_source', 'gate', '70',   'number', '弱趋势打分门槛', '25≤ADX≤35 时放行最低分（÷scale）', 'number', 47, 'global'),
  ('co.gate.weak.lot',          'co_source', 'gate', '1.0',  'number', '弱趋势仓位系数', '标准仓位乘数（0.2-1.0）', 'number', 48, 'global'),
  ('co.gate.weak.sl_atr',       'co_source', 'gate', '0.6',  'number', '弱趋势SL ATR倍数', '止损 ATR 倍数（0.2-1.0）', 'number', 49, 'global'),
  ('co.gate.weak.rr_min',       'co_source', 'gate', '1.5',  'number', '弱趋势最低盈亏比', '最低风险回报比（1.0-3.0）', 'number', 50, 'global'),
  ('co.gate.range.block',       'co_source', 'gate', 'true', 'bool',   '震荡市拦截开关', 'ADX<20（RANGE/NEUTRAL）时直接拦截全部开仓信号', 'switch', 51, 'global'),
  ('co.gate.shock.trend',       'co_source', 'gate', '80',   'number', '突发波动打分门槛', 'ATR 翻倍时放行最低分（÷scale）', 'number', 52, 'global'),
  ('co.gate.shock.lot',         'co_source', 'gate', '0.5',  'number', '突发波动仓位系数', '标准仓位乘数（0.1-1.0）', 'number', 53, 'global'),
  ('co.gate.shock.sl_atr',      'co_source', 'gate', '0.7',  'number', '突发波动SL ATR倍数', '止损 ATR 倍数（0.3-1.0）', 'number', 54, 'global'),
  ('co.gate.shock.rr_min',      'co_source', 'gate', '1.5',  'number', '突发波动最低盈亏比', '最低风险回报比（1.0-3.0）', 'number', 55, 'global'),
  ('co.gate.risk.high_offset',  'co_source', 'gate', '10',   'number', '高风险日门槛偏移', 'risk=high 时门槛加此值（÷scale，0-20）', 'number', 56, 'global'),
  ('co.gate.risk.med_offset',   'co_source', 'gate', '5',    'number', '中风险日门槛偏移', 'risk=med 时门槛加此值（÷scale，0-15）', 'number', 57, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 共源信号 — G4 批量 AI 调用（紫 #8b5cf6）
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('co.batch.enabled',              'co_source', 'batch', 'true',  'bool',   '启用每日批量分析', '关闭后信号塔退回到逐信号 AI 调用', 'switch', 60, 'global'),
  ('co.batch.call1_time',           'co_source', 'batch', '06:00', 'text',   '调用①时间(BJ)', '行情标注+参数校验+漏洞复盘', 'text', 61, 'global'),
  ('co.batch.call2_time',           'co_source', 'batch', '18:00', 'text',   '调用②时间(BJ)', '欧盘开盘前风险预判', 'text', 62, 'global'),
  ('co.batch.call3_enabled',        'co_source', 'batch', 'true',  'bool',   '启用周日周复盘', '每周日 20:00 全周策略回顾', 'switch', 63, 'global'),
  ('co.batch.emergency.enabled',    'co_source', 'batch', 'true',  'bool',   '启用极端行情补充调用', 'ATR 翻倍或连续亏损时临时调 AI', 'switch', 64, 'global'),
  ('co.batch.emergency.max_per_day','co_source', 'batch', '2',     'int',    '补充调用每日上限', '极端补充调用每日最多次数（1-4）', 'number', 65, 'global'),
  ('co.batch.emergency.cooldown_h', 'co_source', 'batch', '2',     'int',    '补充调用冷却(h)', '同条件触发间隔（1-6）', 'number', 66, 'global'),
  ('co.batch.lookback_days',        'co_source', 'batch', '30',    'int',    '批量分析回看天数', 'DeepSeek 接收的历史数据天数（15-60）', 'number', 67, 'global'),
  ('co.batch.timeout_sec',          'co_source', 'batch', '60',    'int',    '批量调用超时(秒)', '单次 DeepSeek 批量调用最长等待（30-120）', 'number', 68, 'global'),
  ('co.batch.calendar_source',      'co_source', 'batch', 'deepseek_knowledge', 'select', '财经日历来源', 'deepseek_knowledge=AI自判 / none=关闭', 'select', 69, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 共源信号 — G5 Optuna 自动调参（青 #06b6d4）
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('co.optuna.enabled',         'co_source', 'optuna', 'true',  'bool',   '启用 Optuna 自动调参', '关闭后只用面板手动参数', 'switch', 70, 'global'),
  ('co.optuna.train_days',      'co_source', 'optuna', '60',    'int',    '训练集天数', 'Optuna 回测训练窗口（30-90）', 'number', 71, 'global'),
  ('co.optuna.test_days',       'co_source', 'optuna', '15',    'int',    '测试集天数', '测试窗口（7-30）', 'number', 72, 'global'),
  ('co.optuna.trials',          'co_source', 'optuna', '100',   'int',    '每轮试验次数', 'TPE sampler trials（50-200）', 'number', 73, 'global'),
  ('co.optuna.target',          'co_source', 'optuna', 'sharpe_ratio', 'select', '优化目标', 'sharpe_ratio / sortino_ratio / calmar_ratio', 'select', 74, 'global'),
  ('co.optuna.max_drawdown_pct', 'co_source', 'optuna', '15',   'number', '最大回撤约束(%)', '任何参数组回撤超此值即淘汰（5-30）', 'number', 75, 'global'),
  ('co.optuna.min_trades',      'co_source', 'optuna', '60',    'int',    '最少有效交易', '测试期内交易次数低于此值淘汰（30-100）', 'number', 76, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 共源信号 — G6 执行增强 / FORCE_CLOSE（红 #ef4444）
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('co.exec.force_close_enabled', 'co_source', 'exec', 'true', 'bool',   '启用 FORCE_CLOSE 信号', 'H1 趋势反转时发布强制平仓信号', 'switch', 80, 'global'),
  ('co.exec.fc_close_mode',       'co_source', 'exec', 'all',  'select', '强反转平仓模式', 'all=全平 / half=半平 / half_on_weakening=走弱半平+翻转全平', 'select', 81, 'global'),
  ('co.exec.fc_bar_confirm',      'co_source', 'exec', '3',    'int',    '反转确认 bar 数', '连续 N 根 M5 bar 确认新方向后触发（2-5）', 'number', 82, 'global'),
  ('co.exec.fc_adx_min',          'co_source', 'exec', '30',   'number', '反转最低 ADX', 'ADX 低于此值不触发 FORCE_CLOSE（20-40）', 'number', 83, 'global'),
  ('co.exec.position_check_min',  'co_source', 'exec', '15',   'int',    '持仓检查间隔(分钟)', '每 N 分钟检查一次 H1 趋势是否翻转（5-30）', 'number', 84, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 信号模型选择开关（约束 ③ 向后兼容 fallback）
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('signal.active_model', 'signal_tower', 'core', 'default', 'select', '信号模型', 'default=默认规则引擎 / co_source=共源信号（双源增强）', 'select', 90, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ═══════════════════════════════════════════════════════════════
-- hcm_ai 模式 + 3 张标注/参数/漏洞表（数据层，P1b/P2/P3 使用）
-- ═══════════════════════════════════════════════════════════════
CREATE SCHEMA IF NOT EXISTS hcm_ai;

-- ① 标注样本表：DeepSeek 每日批量标注的 K 线标签，供校准因子(§4.1.2)更新
CREATE TABLE IF NOT EXISTS hcm_ai.labeled_samples (
    id            BIGSERIAL PRIMARY KEY,
    symbol        VARCHAR(20)  NOT NULL,
    timeframe     VARCHAR(10)  NOT NULL DEFAULT 'M5',
    bar_time      TIMESTAMPTZ  NOT NULL,          -- K 线开盘时间
    m5_regime     VARCHAR(20)  NOT NULL,          -- PRE_TREND/TREND/TREND_FADE/RANGE/NEUTRAL
    direction     VARCHAR(10)  NOT NULL,          -- BUY/SELL/NO_TRADE
    label         VARCHAR(20)  NOT NULL,          -- win/loss/unknown（事后回看结果）
    raw_score     DOUBLE PRECISION,
    calibrated    DOUBLE PRECISION,               -- 校准后得分（用于 factor 计算）
    note          TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (symbol, timeframe, bar_time)
);
CREATE INDEX IF NOT EXISTS ix_labeled_samples_bar ON hcm_ai.labeled_samples (symbol, bar_time);
CREATE INDEX IF NOT EXISTS ix_labeled_samples_regime ON hcm_ai.labeled_samples (m5_regime, label);

-- ② 参数历史表：Optuna / DeepSeek 挑中的最优参数组（含来源标记）
CREATE TABLE IF NOT EXISTS hcm_ai.param_history (
    id             BIGSERIAL PRIMARY KEY,
    run_date       DATE         NOT NULL,
    source         VARCHAR(20)  NOT NULL,          -- optuna / deepseek_pick / manual
    params_json    JSONB        NOT NULL,           -- 写入 hcm_config.metadata 的键集合
    sharpe         DOUBLE PRECISION,
    max_drawdown   DOUBLE PRECISION,
    stability_score DOUBLE PRECISION,               -- DeepSeek 稳定性评分
    note           TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_param_history_date ON hcm_ai.param_history (run_date, source);

-- ③ 漏洞复盘规则表：调用①"漏洞复盘"产出的规则（可触发配置/逻辑调整）
CREATE TABLE IF NOT EXISTS hcm_ai.gap_rules (
    id             BIGSERIAL PRIMARY KEY,
    rule_text      TEXT         NOT NULL,
    severity       VARCHAR(10)  NOT NULL DEFAULT 'medium',  -- high/medium/low
    status         VARCHAR(10)  NOT NULL DEFAULT 'open',     -- open/resolved/ignored
    source_call    VARCHAR(10)  NOT NULL DEFAULT 'call1',    -- call1/call2/call3/emergency
    resolved_at    TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_gap_rules_status ON hcm_ai.gap_rules (status, severity);

COMMIT;
