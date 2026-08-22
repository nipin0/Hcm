-- ═══════════════════════════════════════════════════════════════
-- 0018_hexp_extreme_auto.sql
-- 极值护栏自动开/关（2026-08-19）：auto_mode=auto 时按 M5 regime 自动推导硬封开关。
--   off  = 沿用人工开关 reversal_enabled（默认，行为与历史一致）
--   auto = regime ∈ auto_on_regimes(RANGE/NEUTRAL) → 开；∈ auto_off_regimes(TREND/PRE_TREND) → 关
-- 键集合与 signal_tower/hexp_engine.py _DEFAULTS、web/api/hexp.py HEXP_KEYS 对齐。
-- 幂等：可重复执行。
-- ═══════════════════════════════════════════════════════════════

BEGIN;

INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, current_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('hexp.extreme.auto_mode',        'hexp', 'extreme', 'off',   'off',   'string', '极值护栏自动模式',
   'off=沿用人工开关；auto=按 M5 体制自动开/关极值硬封', 'select', 210, 'global'),
  ('hexp.extreme.auto_on_regimes',  'hexp', 'extreme', 'RANGE,NEUTRAL', 'RANGE,NEUTRAL', 'string', '自动开启体制',
   '逗号分隔；regime 命中即自动开启极值硬封（震荡/极值行情）', 'text', 211, 'global'),
  ('hexp.extreme.auto_off_regimes', 'hexp', 'extreme', 'TREND,PRE_TREND', 'TREND,PRE_TREND', 'string', '自动关闭体制',
   '逗号分隔；regime 命中即自动关闭极值硬封（趋势确认行情）', 'text', 212, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- 固化 current_value（缺失/空才补，不覆盖已调优值）
UPDATE hcm_config.metadata
   SET current_value = default_value
 WHERE config_key IN ('hexp.extreme.auto_mode',
                      'hexp.extreme.auto_on_regimes',
                      'hexp.extreme.auto_off_regimes')
   AND (current_value IS NULL OR btrim(current_value) = '');

COMMIT;

-- Redis 侧双写（如容器内可连 Redis，否则靠 config_provider 热重载回源 PG）：
--   HSET hcm:config:v2 hexp.extreme.auto_mode off
--   HSET hcm:config:v2 hexp.extreme.auto_on_regimes "RANGE,NEUTRAL"
--   HSET hcm:config:v2 hexp.extreme.auto_off_regimes "TREND,PRE_TREND"
--   PUBLISH hcm:config:invalidate hexp.extreme.auto_mode
--   PUBLISH hcm:config:invalidate hexp.extreme.auto_on_regimes
--   PUBLISH hcm:config:invalidate hexp.extreme.auto_off_regimes
-- 或最简单：HDEL 三键 → config_provider 下次读取自动回源 PG 重建合法值。
--
-- 验证：
--   SELECT config_key, current_value, value_type FROM hcm_config.metadata
--    WHERE config_key LIKE 'hexp.extreme.auto_%';
--   docker compose restart hcm-signal-tower hcm-web
