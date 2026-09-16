-- ═══════════════════════════════════════════════════════════════
-- 0030_trend_governance.sql
-- 2026-09-11 【A/B/C 趋势优先治理】治「上涨趋势中发 SELL」的架构性缺陷。
--
-- 背景（附证据）：f_rsi(RSI>70→-1) 与 f_pos(顶部→-1) 为均值回归语义，anti_cancel
--   在 NEUTRAL/RANGE 下又升权 rsi、降权 ma → 强趋势高位 dir_sum 转负 → 逆势开空。
--   实证 2026-09-11 12:52 SELL@4379（当时 h1=BULLISH/UP、RSI=74.7）→ 该单 -20 止损；
--   当日全天 0 笔 BUY、54 笔 SELL，SELL 净亏 -125.61。
--
-- A（止血，本迁移生效）：counter_block_threshold 0.55 → 0.40，使「单周期强趋势 +
--   逆势」也能被强共振硬拦（原 0.55 叠加 min_periods=2 的单周期置信折半 → 漏拦）。
--   ⚠️ 回退：UPDATE current_value='0.55'。
-- B/C（新键，默认 shadow/off = 零行为变化）：仅登记 seed，供面板热调。
-- 幂等：BOOTSTRAP ON CONFLICT DO NOTHING；A 为显式 UPDATE。
-- ═══════════════════════════════════════════════════════════════

BEGIN;

UPDATE hcm_config.metadata
   SET current_value = '0.40', updated_at = now()
 WHERE config_key = 'hexp.resonance.counter_block_threshold';

INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, current_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('hexp.anti_cancel.enabled', 'hexp', 'direction', 'true', 'true', 'boolean',
   '抗抵消调制总开关',
   'NEUTRAL/RANGE + hurst<0.5 时按位置调制方向权重（降 ma、升 rsi/hurst/mm）。',
   'switch', 200, 'global'),

  ('hexp.anti_cancel.curve', 'hexp', 'direction', '1.0', '1.0', 'number',
   '抗抵消·位置调制强度',
   '指数：越大则越极端位置的反向加权越强。',
   'number', 201, 'global'),

  ('hexp.anti_cancel.ma_floor', 'hexp', 'direction', '0.35', '0.35', 'number',
   '抗抵消·ma 底权',
   '趋势因子 ma 的权重下限（防趋势因子被彻底压制而失聪）。',
   'number', 202, 'global'),

  ('hexp.anti_cancel.trend_guard', 'hexp', 'direction', 'false', 'false', 'boolean',
   'B2·趋势态禁抗抵消',
   'true=主周期状态机确认 TREND_UP/DOWN 时跳过 anti_cancel 均值回归调制（不在趋势里做反转加权）。默认 false=零行为变化。',
   'switch', 203, 'global'),

  ('hexp.trend_rsi_mode', 'hexp', 'direction', 'shadow', 'shadow', 'string',
   'B1·趋势态 RSI 语义',
   'off=旧语义(RSI>70 推空)；shadow=只观测不改裁决（默认）；on=确认趋势态下超买不再推空。',
   'string', 204, 'global'),

  ('hexp.direction_hysteresis_ttl_bars', 'hexp', 'direction', '0', '0', 'number',
   'B4·方向迟滞 TTL(根)',
   '连续「保守维持」超过该根数即允许翻向，治方向长期粘滞；0=关闭（默认）。',
   'number', 205, 'global'),

  ('hexp.trend_priority_mode', 'hexp', 'direction', 'shadow', 'shadow', 'string',
   'C·趋势优先模式',
   'off=不启用；shadow=只观测（默认）；on=确认趋势态(主周期 TREND_UP/DOWN)时禁止逆势开单。',
   'string', 206, 'global')
ON CONFLICT (config_key) DO NOTHING;

COMMIT;
