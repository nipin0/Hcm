-- ═══════════════════════════════════════════════════════════════
-- HCM v2 — config_metadata 初始化数据 (180+ 参数)
-- 版本: v1.0 | 基于 PRD v1.3 §9.2 参数清单
-- 执行时机: PostgreSQL 容器首次启动时自动执行
-- ═══════════════════════════════════════════════════════════════

BEGIN;

-- ============================================================
-- 系统级配置 — 用户权限
-- ============================================================

INSERT INTO hcm_config.metadata (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
('auth_session_timeout_min', 'system', 'auth', '480', 'int', '登录超时（分钟）', '用户无操作后自动登出的时间', 'number', 1, 'global'),
('auth_max_failed_attempts', 'system', 'auth', '5', 'int', '最大失败次数', '连续登录失败达到此次数后锁定账户', 'number', 2, 'global'),
('auth_lockout_minutes', 'system', 'auth', '30', 'int', '锁定时间（分钟）', '账户被锁定后自动解锁的时间', 'number', 3, 'global'),
('auth_jwt_secret', 'system', 'auth', '', 'string', 'JWT 密钥', '用于签发和验证 JWT Token 的密钥', 'password', 4, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 系统级配置 — DeepSeek AI
-- ============================================================

INSERT INTO hcm_config.metadata (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope, is_sensitive) VALUES
('deepseek_api_key', 'system', 'ai', '', 'string', 'DeepSeek API Key', 'DeepSeek API 访问密钥', 'password', 10, 'global', true),
('deepseek_base_url', 'system', 'ai', 'https://api.deepseek.com', 'string', 'DeepSeek Base URL', 'API 基础地址', 'text', 11, 'global', false),
('deepseek_model', 'system', 'ai', 'deepseek-chat', 'string', 'DeepSeek 模型', '使用的模型名称', 'text', 12, 'global', false),
('deepseek_direction_mode', 'system', 'ai', 'advisory', 'string', 'AI 研判模式', 'advisory=建议 / override=覆盖', 'select', 13, 'global', false),
('ai_call_timeout_sec', 'system', 'ai', '15', 'int', 'AI 调用超时（秒）', 'DeepSeek API 调用最大等待时间', 'number', 14, 'global', false),
('ai_max_retries', 'system', 'ai', '1', 'int', 'AI 最大重试次数', 'DeepSeek 调用失败后的最大重试次数', 'number', 15, 'global', false),
('signal_concurrency_max', 'system', 'ai', '1', 'int', '信号生产并发数', '同时生产的信号任务最大数量', 'number', 16, 'global', false),
('external_factor_prompt_version', 'system', 'ai', 'v2_compact', 'string', '外部因子 Prompt 版本', '赋分 Prompt 模板版本', 'select', 17, 'global', false)
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 信号塔 — 核心参数
-- ============================================================

INSERT INTO hcm_config.metadata (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
('indicator_weight_mode', 'signal_tower', 'core', 'ai_dynamic', 'string', '权重模式', 'ai_dynamic=AI动态 / manual=手动', 'select', 20, 'global'),
('manual_regime_score', 'signal_tower', 'core', '', 'int', '手动市况分', '0-100, 留空=AI自主判定', 'slider', 21, 'symbol'),
('score_threshold', 'signal_tower', 'core', '0.50', 'float', '评分阈值', '信号评分最低通过线', 'slider', 22, 'global'),
('signal_cooldown_seconds', 'signal_tower', 'core', '300', 'int', '信号冷却时间（旧版）', '[DEPRECATED] 五级模型差异化冷却替代', 'number', 23, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 预评分门控参数
-- ============================================================

INSERT INTO hcm_config.metadata (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
('pre_score_tier_low', 'signal_tower', 'inference', '0.40', 'float', '低分档阈值', 'pre_score < 此值归为低分档', 'slider', 30, 'global'),
('pre_score_tier_mid', 'signal_tower', 'inference', '0.55', 'float', '中分档阈值', 'pre_score < 此值归为中分档', 'slider', 31, 'global'),
('pre_score_tier_high', 'signal_tower', 'inference', '0.65', 'float', '高分档阈值', 'pre_score >= 此值归为高分档', 'slider', 32, 'global'),
('composite_bypass_floor', 'signal_tower', 'inference', '0.80', 'float', '综合 bypass 下限', 'composite_score >= 此值可 bypass AI', 'slider', 33, 'global'),
('osc_bypass_floor', 'signal_tower', 'inference', '0.70', 'float', '振荡器 bypass 下限', '振荡器信号 >= 此值可 bypass AI', 'slider', 34, 'global'),
('direction_conflict_threshold', 'signal_tower', 'inference', '0.55', 'float', '方向冲突阈值', '多周期方向分歧超过此值触发冲突处理', 'slider', 35, 'global'),
('default_atr_xauusd_m5', 'signal_tower', 'inference', '6.0', 'float', 'XAUUSD ATR 默认值', 'XAUUSD M5 默认 ATR(点)', 'number', 36, 'symbol'),
('macd_trend_divisor', 'signal_tower', 'inference', '1.5', 'float', 'MACD 趋势除数', 'MACD 趋势信号标准化除数', 'number', 37, 'global'),
('ma_diff_divisor', 'signal_tower', 'inference', '30.0', 'float', 'MA 差值除数', 'MA 排列差值标准化除数', 'number', 38, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 五级市况模型 — PRE_TREND
-- ============================================================

INSERT INTO hcm_config.metadata (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
('pretrend_adx_rising_bars', 'signal_tower', 'regime', '3', 'int', 'PRE_TREND ADX 连续上升 K 线数', 'ADX 连续上升几根K线判定预启动', 'number', 40, 'global'),
('pretrend_breakout_factor', 'signal_tower', 'regime', '1.005', 'float', 'PRE_TREND 突破因子', 'close > high_20bar * 此值判定为突破', 'number', 41, 'global'),
('pretrend_breakout_lookback', 'signal_tower', 'regime', '20', 'int', 'PRE_TREND 突破回溯 K 线数', '突破检测的回溯窗口', 'number', 42, 'global'),
('pretrend_threshold_floor', 'signal_tower', 'regime', '0.42', 'float', 'PRE_TREND 阈值下限', '预启动信号评分最低阈值', 'slider', 43, 'global'),
('pretrend_cooldown_seconds', 'signal_tower', 'regime', '180', 'int', 'PRE_TREND 冷却时间', '预启动阶段信号冷却（秒）', 'number', 44, 'global'),
('pretrend_adx_filter_weight', 'signal_tower', 'regime', '0.50', 'float', 'PRE_TREND ADX 方向过滤权重', '预启动阶段 ADX 方向偏置过滤权重', 'slider', 45, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 五级市况模型 — TREND
-- ============================================================

INSERT INTO hcm_config.metadata (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
('regime_adx_trend', 'signal_tower', 'regime', '24', 'float', '趋势 ADX 阈值', 'ADX >= 此值判定为趋势市', 'number', 50, 'global'),
('trend_cooldown_seconds', 'signal_tower', 'regime', '120', 'int', '趋势市冷却（秒）', '趋势确认后信号冷却时间', 'number', 51, 'global'),
('trend_same_dir_bypass_score', 'signal_tower', 'regime', '0.60', 'float', '同向冷却豁免阈值', 'pre_score >= 此值可豁免冷却', 'slider', 52, 'global'),
('trend_same_dir_mid_score', 'signal_tower', 'regime', '0.50', 'float', '同向中质信号阈值', 'pre_score >= 此值冷却减半', 'slider', 53, 'global'),
('trend_same_dir_mid_cooldown', 'signal_tower', 'regime', '60', 'int', '同向中质信号冷却（秒）', '中质同向信号冷却减半后秒数', 'number', 54, 'global'),
('trend_strong_adx_threshold', 'signal_tower', 'regime', '28.0', 'float', '强势趋势 ADX 阈值', 'ADX >= 此值视为强趋势', 'number', 55, 'global'),
('trend_strong_threshold_offset', 'signal_tower', 'regime', '-0.05', 'float', '强势趋势阈值偏移', '强趋势时 score_threshold 降低值', 'number', 56, 'global'),
('trend_reverse_cooldown_seconds', 'signal_tower', 'regime', '300', 'int', '逆势信号冷却（秒）', '反向信号冷却时间', 'number', 57, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 五级市况模型 — TREND_FADE
-- ============================================================

INSERT INTO hcm_config.metadata (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
('fade_adx_falling_bars', 'signal_tower', 'regime', '3', 'int', 'FADE ADX 连续下降判定', 'ADX 连续下降几根 K 线判定衰竭', 'number', 60, 'global'),
('fade_bbw_ratio_max', 'signal_tower', 'regime', '1.0', 'float', 'FADE BBW 比值上界', 'BBW/MA20 < 此值配合衰竭判定', 'number', 61, 'global'),
('fade_threshold_ceiling', 'signal_tower', 'regime', '0.78', 'float', 'FADE 阈值上限', '衰竭阶段评分最高阈值', 'slider', 62, 'global'),
('fade_cooldown_seconds', 'signal_tower', 'regime', '300', 'int', 'FADE 冷却时间', '趋势衰竭冷却（秒）', 'number', 63, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 五级市况模型 — RANGE
-- ============================================================

INSERT INTO hcm_config.metadata (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
('regime_adx_range', 'signal_tower', 'regime', '22.0', 'float', '震荡 ADX 上限', 'ADX < 此值判定为震荡市', 'number', 70, 'global'),
('regime_bbw_contract', 'signal_tower', 'regime', '0.8', 'float', 'BBW 收缩阈值', 'BBW <= 此值辅助判定震荡', 'number', 71, 'global'),
('range_boundary_low', 'signal_tower', 'regime', '0.20', 'float', '区间底部 %b_range', '价格在区间下沿位置', 'slider', 72, 'global'),
('range_boundary_high', 'signal_tower', 'regime', '0.80', 'float', '区间顶部 %b_range', '价格在区间上沿位置', 'slider', 73, 'global'),
('range_bonus_boundary', 'signal_tower', 'regime', '0.12', 'float', '边界加分', '价格在区间边界时的加分值', 'number', 74, 'global'),
('range_bonus_near_boundary', 'signal_tower', 'regime', '0.05', 'float', '近边界加分', '价格接近区间边界时的加分值', 'number', 75, 'global'),
('range_boundary_cooldown_seconds', 'signal_tower', 'regime', '0', 'int', '边界冷却豁免', '区间边界信号冷却（0=不冷却）', 'number', 76, 'global'),
('range_adx_exemption_threshold', 'signal_tower', 'regime', '15.0', 'float', 'ADX 完全豁免阈值', 'ADX < 此值完全豁免 ADX 过滤', 'number', 77, 'global'),
('range_adx_weak_weight', 'signal_tower', 'regime', '0.30', 'float', '弱 ADX 过滤权重', 'ADX 15-20 之间 ADX 过滤权重', 'slider', 78, 'global'),
('range_threshold_floor', 'signal_tower', 'regime', '0.40', 'float', '震荡阈值下限', '震荡市信号评分最低阈值', 'slider', 79, 'global'),
('range_lookback_bars', 'signal_tower', 'regime', '30', 'int', '区间识别回溯 K 线数', '震荡区间识别回看 K 线数', 'number', 80, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 市况切换确认参数
-- ============================================================

INSERT INTO hcm_config.metadata (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
('switch_pretrend_confirm_bars', 'signal_tower', 'regime_switch', '0', 'int', '→PRE_TREND 确认 K 线数', '进入预启动需要确认的 K 线数（0=即时）', 'number', 90, 'global'),
('switch_pretrend_lock_bars', 'signal_tower', 'regime_switch', '0', 'int', 'PRE_TREND 锁仓 K 线数', '预启动阶段锁仓 K 线数', 'number', 91, 'global'),
('switch_trend_confirm_bars', 'signal_tower', 'regime_switch', '2', 'int', 'PRE_TREND→TREND 确认 K 线数', '预启动转入趋势需要确认的 K 线数', 'number', 92, 'global'),
('switch_trend_lock_bars', 'signal_tower', 'regime_switch', '1', 'int', 'TREND 锁仓 K 线数', '趋势阶段锁仓 K 线数', 'number', 93, 'global'),
('switch_fade_confirm_bars', 'signal_tower', 'regime_switch', '2', 'int', '→FADE 确认 K 线数', '进入衰竭阶段需要确认的 K 线数', 'number', 94, 'global'),
('switch_fade_lock_bars', 'signal_tower', 'regime_switch', '0', 'int', 'FADE 锁仓 K 线数', '衰竭阶段锁仓 K 线数', 'number', 95, 'global'),
('switch_range_confirm_bars', 'signal_tower', 'regime_switch', '2', 'int', '→RANGE 确认 K 线数', '进入震荡需要确认的 K 线数', 'number', 96, 'global'),
('switch_range_lock_bars', 'signal_tower', 'regime_switch', '1', 'int', 'RANGE 锁仓 K 线数', '震荡阶段锁仓 K 线数', 'number', 97, 'global'),
('neutral_threshold_offset', 'signal_tower', 'regime_switch', '0.10', 'float', '中立阈值偏移', 'NEUTRAL 评分阈值的偏移量', 'number', 98, 'global'),
('neutral_cooldown_seconds', 'signal_tower', 'regime_switch', '300', 'int', '中立冷却时间', '中立阶段信号冷却（秒）', 'number', 99, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 数据源与调度参数
-- ============================================================

INSERT INTO hcm_config.metadata (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
('signal_timeframes', 'signal_tower', 'datasource', 'M5,M15', 'string', '信号推理周期列表', '逗号分隔的周期列表', 'text', 100, 'global'),
('adx_range_skip_multi_tf', 'signal_tower', 'datasource', '1', 'int', 'ADX 震荡跳过多周期', '震荡市是否跳过多周期确认', 'switch', 101, 'global'),
('adx_range_cooldown_minutes', 'signal_tower', 'datasource', '15', 'int', 'ADX 震荡冷却（分钟）', '震荡市额外冷却时间', 'number', 102, 'global'),
('kline_not_ready_retry_sec', 'signal_tower', 'datasource', '3', 'int', 'K线未就绪重试间隔（秒）', 'K线数据未就绪时的重试间隔', 'number', 103, 'global'),
('kline_not_ready_max_retries', 'signal_tower', 'datasource', '3', 'int', 'K线未就绪最大重试', 'K线数据未就绪的最大重试次数', 'number', 104, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 指标周期参数（P0: 接入 config_provider，运行时可调）
-- ============================================================

INSERT INTO hcm_config.metadata (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
('indicator_rsi_period', 'signal_tower', 'indicator', '14', 'int', 'RSI 周期', 'RSI 计算回看周期', 'number', 210, 'global'),
('indicator_macd_fast', 'signal_tower', 'indicator', '12', 'int', 'MACD 快线周期', 'MACD 快线 EMA 周期', 'number', 211, 'global'),
('indicator_macd_slow', 'signal_tower', 'indicator', '26', 'int', 'MACD 慢线周期', 'MACD 慢线 EMA 周期', 'number', 212, 'global'),
('indicator_macd_signal', 'signal_tower', 'indicator', '9', 'int', 'MACD 信号线周期', 'MACD 信号线 EMA 周期', 'number', 213, 'global'),
('indicator_adx_period', 'signal_tower', 'indicator', '14', 'int', 'ADX 周期', 'ADX 计算回看周期', 'number', 214, 'global'),
('indicator_boll_period', 'signal_tower', 'indicator', '20', 'int', '布林带周期', '布林带 SMA 周期', 'number', 215, 'global'),
('indicator_boll_std', 'signal_tower', 'indicator', '2.0', 'float', '布林带标准差倍数', '布林带带宽 = 倍数 × 标准差', 'number', 216, 'global'),
('indicator_stoch_k', 'signal_tower', 'indicator', '14', 'int', 'Stochastic %K 周期', '随机指标 %K 周期', 'number', 217, 'global'),
('indicator_stoch_d', 'signal_tower', 'indicator', '3', 'int', 'Stochastic %D 周期', '随机指标 %D SMA 周期', 'number', 218, 'global'),
('indicator_stoch_smooth', 'signal_tower', 'indicator', '3', 'int', 'Stochastic 平滑周期', '随机指标平滑周期', 'number', 219, 'global'),
('indicator_ma_short', 'signal_tower', 'indicator', '10', 'int', '短期均线周期', '短期均线(SMA)周期', 'number', 220, 'global'),
('indicator_ma_long', 'signal_tower', 'indicator', '30', 'int', '长期均线周期', '长期均线(SMA)周期', 'number', 221, 'global'),
('range_bbw_max', 'signal_tower', 'regime', '1.0', 'float', '震荡 BBW 上界', 'BBW <= 此值判定为震荡市（RegimeClassifier.range_bbw_max）', 'number', 125, 'global'),
('signal_tower.config_reload_interval', 'signal_tower', 'system', '30', 'int', '配置热重载间隔（秒）', '参数变更后最长生效延迟；调小=更敏捷', 'number', 126, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- P1: 波动率自适应（信号塔敏捷度治理）
-- ============================================================
INSERT INTO hcm_config.metadata (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
('regime_vol_adapt_enable', 'signal_tower', 'regime', 'false', 'bool', '波动率自适应开关', '按相对带宽(BBW/BBW_MA20)动态缩放ADX阈值与确认bar：高波动收紧防抖、低波动放松快响应', 'switch', 90, 'global'),
('regime_vol_adapt_scale', 'signal_tower', 'regime', '0.15', 'float', '波动率缩放幅度', '缩放系数(dev*scale, 裁剪±0.4)；0=关闭。越大越灵敏', 'slider', 91, 'global'),
('regime_vol_adapt_band_ref', 'signal_tower', 'regime', '1.0', 'float', '参考带宽基准', 'vol_ratio基准(=BBW/BBW_MA20)；偏离此值才缩放', 'number', 92, 'global'),
('cooldown_vol_enable', 'signal_tower', 'regime', 'false', 'bool', '冷却波动率自适应', '按相对带宽动态缩放信号冷却：高波动缩短(快再入场)、低波动延长(减噪)', 'switch', 93, 'global'),
('cooldown_vol_scale', 'signal_tower', 'regime', '0.20', 'float', '冷却缩放幅度', '冷却缩放系数((vol_ratio-1)*scale, 裁剪±0.3)；0=关闭', 'slider', 94, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 看门狗与健康检查
-- ============================================================

INSERT INTO hcm_config.metadata (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
('watchdog_enabled', 'watchdog', 'core', 'true', 'bool', '启用看门狗', '是否启用四维看门狗监控', 'switch', 110, 'global'),
('watchdog_interval_sec', 'watchdog', 'core', '30', 'int', '看门狗检查间隔（秒）', '看门狗执行健康检查的间隔', 'number', 111, 'global'),
('watchdog_stall_threshold_sec', 'watchdog', 'core', '300', 'int', '主循环卡死阈值（秒）', '主循环无响应超过此时长触发告警', 'number', 112, 'global'),
('watchdog_loop_timeout_sec', 'watchdog', 'core', '600', 'int', '单次循环最大时长（秒）', '单次信号生产循环最大允许时长', 'number', 113, 'global'),
('upstream_check_interval_sec', 'watchdog', 'core', '30', 'int', '上游健康检查间隔（秒）', '检查 PG/Redis/DeepSeek 等上游服务的间隔', 'number', 114, 'global'),
('upstream_failure_threshold', 'watchdog', 'core', '3', 'int', '上游失败告警阈值', '上游连续失败N次触发告警', 'number', 115, 'global'),
('deepseek_degradation_threshold', 'watchdog', 'core', '5', 'int', 'DeepSeek 熔断阈值', 'DeepSeek 连续失败 N 次自动熔断', 'number', 116, 'global'),
('deepseek_auto_recover_sec', 'watchdog', 'core', '1800', 'int', '熔断自动恢复间隔（秒）', '熔断后自动尝试恢复的等待时间', 'number', 117, 'global'),
('kline_stale_threshold_sec', 'watchdog', 'core', '600', 'int', 'K线数据滞后阈值（秒）', 'K线数据超过此时长视为过期', 'number', 118, 'global'),
('idle_sleep_sec', 'watchdog', 'core', '5', 'int', '空闲休眠间隔（秒）', '无任务时的休眠时长', 'number', 119, 'global'),
('loop_error_sleep_sec', 'watchdog', 'core', '5', 'int', '错误休眠间隔（秒）', '循环出错后的休眠时长', 'number', 120, 'global'),
('pg_retry_max', 'watchdog', 'core', '3', 'int', 'PG 最大重试次数', 'PostgreSQL 操作最大重试次数', 'number', 121, 'global'),
('redis_retry_max', 'watchdog', 'core', '3', 'int', 'Redis 最大重试次数', 'Redis 操作最大重试次数', 'number', 122, 'global'),
('signal_local_fallback_enabled', 'watchdog', 'core', 'true', 'bool', '本地缓冲启用', 'PG/Redis 不可用时启用本地缓冲', 'switch', 123, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 外部因子采集与赋分
-- ============================================================

INSERT INTO hcm_config.metadata (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
('macro_collect_interval_min', 'market_intel', 'collection', '60', 'int', '宏观采集间隔（分钟）', '宏观因子数据采集频率', 'number', 130, 'global'),
('sentiment_collect_interval_min', 'market_intel', 'collection', '60', 'int', '情绪采集间隔（分钟）', '情绪因子数据采集频率', 'number', 131, 'global'),
('macro_scoring_ttl_sec', 'market_intel', 'scoring', '86400', 'int', '宏观赋分缓存 TTL（秒）', '宏观赋分结果在 Redis 中的有效期', 'number', 132, 'global'),
('sentiment_scoring_ttl_sec', 'market_intel', 'scoring', '86400', 'int', '情绪赋分缓存 TTL（秒）', '情绪赋分结果在 Redis 中的有效期', 'number', 133, 'global'),
('scoring_ai_model', 'market_intel', 'scoring', 'deepseek-chat', 'string', '赋分专用模型', '外部因子赋分使用的 AI 模型', 'select', 134, 'global'),
('scoring_ai_timeout_sec', 'market_intel', 'scoring', '30', 'int', '赋分 AI 超时（秒）', '外部因子赋分 AI 调用超时', 'number', 135, 'global'),
('scoring_retry_max', 'market_intel', 'scoring', '2', 'int', '赋分重试次数', '赋分失败最大重试次数', 'number', 136, 'global'),
('event_warning_advance_min', 'market_intel', 'event', '30', 'int', '事件预警提前（分钟）', '重大事件前提前多久发布预警', 'number', 137, 'global'),
('liquidity_cache_ttl_sec', 'market_intel', 'liquidity', '5', 'int', '流动性缓存 TTL（秒）', '流动性数据在 Redis 中的有效期', 'number', 138, 'global'),
('liquidity_spread_max_pips', 'market_intel', 'liquidity', '5.0', 'float', '最大可接受点差', '超过此点差的品种触发过滤', 'number', 139, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 品种配置
-- ============================================================

INSERT INTO hcm_config.metadata (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
('active_symbols', 'symbol', 'core', '["XAUUSD","BTCUSD"]', 'json', '激活品种列表', '当前激活的交易品种', 'json', 140, 'global'),
('default_symbol', 'symbol', 'core', 'XAUUSD', 'string', '默认品种', 'Dashboard 默认选中的品种', 'select', 141, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 品种级推理参数覆盖 — XAUUSD
-- ============================================================

INSERT INTO hcm_config.metadata (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
('symbol.XAUUSD.inference_tf', 'symbol', 'inference', 'M5', 'string', 'XAUUSD 推理周期', 'XAUUSD 主推理周期', 'select', 150, 'symbol'),
('symbol.XAUUSD.score_threshold', 'symbol', 'inference', '0.50', 'float', 'XAUUSD 评分阈值', 'XAUUSD 信号评分最低通过线', 'slider', 151, 'symbol'),
('symbol.XAUUSD.cooldown_seconds', 'symbol', 'inference', '300', 'int', 'XAUUSD 冷却时间', 'XAUUSD 信号冷却秒数', 'number', 152, 'symbol'),
('symbol.XAUUSD.atr_default', 'symbol', 'inference', '6.0', 'float', 'XAUUSD ATR 默认值', 'XAUUSD 默认 ATR（点）', 'number', 153, 'symbol'),
('symbol.XAUUSD.max_spread_pips', 'symbol', 'inference', '5.0', 'float', 'XAUUSD 最大点差', 'XAUUSD 最大可接受点差', 'number', 154, 'symbol'),
('symbol.XAUUSD.max_positions', 'symbol', 'inference', '3', 'int', 'XAUUSD 最大持仓', 'XAUUSD 最大同时持仓数', 'number', 155, 'symbol'),
('symbol.XAUUSD.max_daily_trades', 'symbol', 'inference', '200', 'int', 'XAUUSD 日最大交易', 'XAUUSD 每日最大交易次数', 'number', 156, 'symbol')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 品种级推理参数覆盖 — BTCUSD
-- ============================================================

INSERT INTO hcm_config.metadata (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
('symbol.BTCUSD.inference_tf', 'symbol', 'inference', 'M15', 'string', 'BTCUSD 推理周期', 'BTCUSD 主推理周期', 'select', 160, 'symbol'),
('symbol.BTCUSD.score_threshold', 'symbol', 'inference', '0.55', 'float', 'BTCUSD 评分阈值', 'BTCUSD 信号评分最低通过线', 'slider', 161, 'symbol'),
('symbol.BTCUSD.cooldown_seconds', 'symbol', 'inference', '600', 'int', 'BTCUSD 冷却时间', 'BTCUSD 信号冷却秒数', 'number', 162, 'symbol'),
('symbol.BTCUSD.atr_default', 'symbol', 'inference', '120.0', 'float', 'BTCUSD ATR 默认值', 'BTCUSD 默认 ATR（点）', 'number', 163, 'symbol'),
('symbol.BTCUSD.max_spread_pips', 'symbol', 'inference', '15.0', 'float', 'BTCUSD 最大点差', 'BTCUSD 最大可接受点差', 'number', 164, 'symbol'),
('symbol.BTCUSD.max_positions', 'symbol', 'inference', '5', 'int', 'BTCUSD 最大持仓', 'BTCUSD 最大同时持仓数', 'number', 165, 'symbol'),
('symbol.BTCUSD.max_daily_trades', 'symbol', 'inference', '50', 'int', 'BTCUSD 日最大交易', 'BTCUSD 每日最大交易次数', 'number', 166, 'symbol')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 类别级默认配置
-- ============================================================

INSERT INTO hcm_config.metadata (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
('category.metals.inference_tf', 'symbol', 'inference', 'M5', 'string', '金属类别默认推理周期', '贵金属类别默认推理周期', 'select', 170, 'global'),
('category.crypto.inference_tf', 'symbol', 'inference', 'M15', 'string', '加密类别默认推理周期', '加密货币类别默认推理周期', 'select', 171, 'global'),
('category.forex.inference_tf', 'symbol', 'inference', 'M5', 'string', '外汇类别默认推理周期', '货币对类别默认推理周期', 'select', 172, 'global'),
('category.metals.atr_default', 'symbol', 'inference', '6.0', 'float', '金属类别 ATR', '贵金属默认 ATR', 'number', 173, 'global'),
('category.crypto.atr_default', 'symbol', 'inference', '120.0', 'float', '加密类别 ATR', '加密货币默认 ATR', 'number', 174, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 风控参数
-- ============================================================

INSERT INTO hcm_config.metadata (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
('risk_max_positions', 'risk', 'limits', '10', 'int', '最大持仓数', '全局最大同时持仓数', 'number', 180, 'global'),
('risk_max_daily_trades', 'risk', 'limits', '200', 'int', '日最大交易数', '全局每日最大交易次数', 'number', 181, 'global'),
('risk_max_daily_loss', 'risk', 'limits', '500', 'float', '日最大亏损', '每日最大亏损金额', 'number', 182, 'global'),
('risk_max_consecutive_losses', 'risk', 'limits', '5', 'int', '最大连续亏损', '连续亏损达到此次数停止交易', 'number', 183, 'global'),
('risk_spread_max_multiplier', 'risk', 'spread', '2.0', 'float', '点差倍数上限', '当前点差超过均值的倍数上限', 'number', 184, 'global'),
('risk_event_lot_scale', 'risk', 'event', '0.5', 'float', '事件期间手数缩放', '重大事件期间的默认手数缩放比例', 'slider', 185, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 跟单参数
-- ============================================================

INSERT INTO hcm_config.metadata (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
('copy_default_lot_mode', 'copy_trading', 'core', 'multiplier', 'string', '默认手数模式', 'FIXED/RISK%/BALANCE_RATIO', 'select', 190, 'global'),
('copy_default_lot_multiplier', 'copy_trading', 'core', '1.0', 'float', '默认手数倍率', '跟单手数倍率', 'number', 191, 'global'),
('copy_dedup_window_sec', 'copy_trading', 'core', '300', 'int', '去重窗口（秒）', '相同信号在此窗口内去重', 'number', 192, 'global'),
('copy_max_sync_delay_ms', 'copy_trading', 'core', '500', 'int', '最大同步延迟（ms）', '跟单同步最大允许延迟', 'number', 193, 'global'),
('copy_enable_gateway_direct', 'copy_trading', 'core', 'true', 'bool', '启用 Gateway 直连', '跟单直连 Gateway gRPC（禁用则走 PUBSUB）', 'switch', 194, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 通知参数
-- ============================================================

INSERT INTO hcm_config.metadata (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
('notification_dingtalk_webhook', 'system', 'notification', '', 'string', '钉钉 Webhook URL', '告警通知钉钉机器人地址', 'text', 200, 'global'),
('notification_email_smtp', 'system', 'notification', '', 'string', '邮件 SMTP', '告警邮件发送服务器', 'text', 201, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- 标记敏感配置
UPDATE hcm_config.metadata SET is_sensitive = true WHERE config_key IN ('auth_jwt_secret', 'deepseek_api_key');

COMMIT;
