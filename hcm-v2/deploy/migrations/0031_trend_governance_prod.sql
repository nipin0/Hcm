-- ═══════════════════════════════════════════════════════════════
-- 0031_trend_governance_prod.sql
-- 2026-09-11 【A/B/C 趋势治理】影子 → 生产切换（用户指令：在生产环境发现问题）。
--
-- 背景：0030 仅登记键 + A 止血（counter_block_threshold=0.40，已在跑）。
--   B1/B2/B3/B4/C 此前为 shadow/off（零行为变化），本迁移切到生产生效。
--
-- ⚠️ 一键回退（任一键独立回退，全部热配置秒级生效）：
--   hexp.trend_rsi_mode               → 'shadow' 或 'off'
--   hexp.anti_cancel.trend_guard      → 'false'
--   hexp.pos_factor.trend_scale       → '0.25'
--   hexp.direction_hysteresis_ttl_bars→ '0'
--   hexp.trend_priority_mode          → 'shadow' 或 'off'
--   hexp.resonance.counter_block_threshold → '0.55'
--
-- 生效开关（代码读取点，均已就位）：
--   B1 hexp_engine.py 趋势态 RSI 语义（_trsi_mode=='on' → 超买不再推空）
--   B2 hexp_engine.py anti_cancel 调制跳过（_ac_trend_guard && _tp_confirmed）
--   B3 hexp_engine.py 位置因子趋势态权重 ×trend_scale（0=停用）
--   B4 hexp_engine.py 迟滞「保守维持」TTL(根)
--   C  hexp_engine.py 确认趋势态 → 禁逆势开单（方向收口 NO_TRADE）
-- ═══════════════════════════════════════════════════════════════

BEGIN;

UPDATE hcm_config.metadata SET current_value='on',     updated_at=now()
 WHERE config_key='hexp.trend_rsi_mode';
UPDATE hcm_config.metadata SET current_value='true',   updated_at=now()
 WHERE config_key='hexp.anti_cancel.trend_guard';
UPDATE hcm_config.metadata SET current_value='0.0',    updated_at=now()
 WHERE config_key='hexp.pos_factor.trend_scale';
UPDATE hcm_config.metadata SET current_value='12',     updated_at=now()
 WHERE config_key='hexp.direction_hysteresis_ttl_bars';
UPDATE hcm_config.metadata SET current_value='on',     updated_at=now()
 WHERE config_key='hexp.trend_priority_mode';

COMMIT;
