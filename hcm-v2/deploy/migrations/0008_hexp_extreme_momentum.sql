-- ═══════════════════════════════════════════════════════════════
-- 0008_hexp_extreme_momentum.sql
-- 和乘幂(hexp)极值动量感知闸门（2026-08-13 升级，替换原无条件硬封）
--   1) 极值区不再无条件封单：仅当「微动量 f_mm 相对原方向回撤」才 NO_TRADE；
--      mm 仍朝原方向 → 允许极值追单（按需求：仅动量回撤时停，可能扫损）。
--   2) 回踩支撑位诊断(B)：现价落入近期摆动低(BUY)/高(SELL) ±support_atr×ATR 带内
--      即标记 extreme_support_pullback，方向交 7 因子重裁（观测/日志，不强制改方向）。
-- 键集合与 signal_tower/hexp_engine.py _DEFAULTS、web/api/hexp.py HEXP_KEYS 对齐
-- 幂等：可重复执行
-- ═══════════════════════════════════════════════════════════════

BEGIN;

-- ============================================================
-- 1) 极值动量感知闸门（A）：替换「高位做多/低位做空」无条件硬封
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, current_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('hexp.extreme.mm_retreat_enabled', 'hexp', 'extreme', 'true', 'true', 'bool',   '极值动量回撤才封',
   '极值区(Donchian 分位触顶/触底)是否启用「仅当 mm 动量回撤才 NO_TRADE」；关=false 退回无条件硬封', 'switch', 200, 'global'),
  ('hexp.extreme.mm_retreat_min',     'hexp', 'extreme', '0.05', '0.05', 'number', '动量回撤阈值',
   'mm 相对原方向的同向分量(=f_mm×dir_sign)低于此值才算「回撤封单」；≥则允许极值追单', 'number', 201, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 2) 回踩支撑位诊断（B）：仅观测标记，不改变方向裁定
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, current_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('hexp.extreme.support_lookback',   'hexp', 'extreme', '20',   '20',   'int',    '回踩枢轴回看棒数',
   'B 诊断：回看窗口取近期摆动低(BUY)/高(SELL)作为支撑枢轴', 'number', 202, 'global'),
  ('hexp.extreme.support_atr',        'hexp', 'extreme', '1.0',  '1.0',  'number', '回踩支撑带宽度(ATR)',
   '现价落入枢轴 ±此值×ATR 内即标记「已回踩到支撑位」，方向交 7 因子重裁', 'number', 203, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- 固化 current_value
UPDATE hcm_config.metadata
   SET current_value = default_value
 WHERE config_key IN ('hexp.extreme.mm_retreat_enabled',
                      'hexp.extreme.mm_retreat_min',
                      'hexp.extreme.support_lookback',
                      'hexp.extreme.support_atr')
   AND (current_value IS NULL OR btrim(current_value) = '');

COMMIT;

-- 执行后需同步 Redis 缓存（配置中心 L2，引擎经 config_provider 懒读后会自动回填；
-- 如需立即生效不依赖首次读，可手动 HSET + PUBLISH）：
--   HSET hcm:config:v2 \
--     hexp.extreme.mm_retreat_enabled true \
--     hexp.extreme.mm_retreat_min 0.05 \
--     hexp.extreme.support_lookback 20 \
--     hexp.extreme.support_atr 1.0
--   PUBLISH hcm:config:v2:updated hexp
--
-- 验证：
--   SELECT config_key, current_value, value_type FROM hcm_config.metadata
--    WHERE config_key LIKE 'hexp.extreme.%';
