-- 0010_hexp_pos_factor.sql — 2026-08-13
-- BUG-1/3 修复配套配置 seed（幂等，可重复执行）：
--   BUG-1: hexp.pos_factor.enabled / weight —— 位置因子参与方向裁决（治"高多低空"）
--   BUG-3: hexp.extreme.mm_retreat_min 0.05→0.20 —— 极值追单要求 M1 动量明显同向
-- BUG-2 (trend 方案 rsi 8) 的 PG 值已就位，本迁移只校验不重写。

INSERT INTO hcm_config.metadata (config_key, current_value, default_value, value_type, category)
VALUES
    ('hexp.pos_factor.enabled', 'true', 'true', 'bool', 'hexp'),
    ('hexp.pos_factor.weight', '0.15', '0.15', 'number', 'hexp')
ON CONFLICT (config_key) DO UPDATE
SET default_value = EXCLUDED.default_value,
    value_type = EXCLUDED.value_type,
    category = EXCLUDED.category;

UPDATE hcm_config.metadata
SET current_value = '0.20', default_value = '0.20', value_type = 'number'
WHERE config_key = 'hexp.extreme.mm_retreat_min';
