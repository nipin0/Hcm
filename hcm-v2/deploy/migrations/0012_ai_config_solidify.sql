BEGIN;

-- 1) 补 ai.lm.veto_floor 新键（来自 0011 迁移新增，2026-08-14 融合闸门修复）
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, current_value, value_type, label, description)
VALUES
  ('ai.lm.veto_floor', 'ai_quality', 'lm', '0.30', '0.30', 'number',
   'AI否决地板阈值',
   'c_ai/100 < 此值才否决（极低分）；否则 HOLD/UPGRADE 放行。避免中等分(如38)被全杀')
ON CONFLICT (config_key) DO NOTHING;

-- 2) 把空值键回填为 default（消除漂移；true/coupled 已由 config_provider 双写覆盖，此处不动）
UPDATE hcm_config.metadata
SET current_value = default_value
WHERE config_key LIKE 'ai.%' AND (current_value IS NULL OR current_value = '');

-- 3) ai.enabled / ai.mode / ai.ds.enabled 当前覆盖值保留（已是 true/coupled/true，不动）
COMMIT;
