-- 0027_quality_recalib_config.sql
-- 【C 项·2026-09-11 质量头校准闭环·配置 seed】
--
-- 依据：docs/设计方案_LightGBM解耦信号级评审与校准治理_20260911.md §校准闭环；
--       记忆 27570774（A 侧车热重载 / B ai_pred_raw 落库 / C recalibrate_quality.py）。
--
-- 本迁移 seed B/C 项所需的 ai.lm.* 配置键，使其可在面板/配置中心管理：
--   · ai.lm.raw_record_enabled    —— 三头原始分落库开关（B 项数据源；默认开）
--   · ai.lm.recalib_window_days   —— 滚动重校准窗口(天)
--   · ai.lm.recalib_min_samples   —— 单头最小样本数(低于则不重校准)
--   · ai.lm.recalib_min_levels    —— 校准器最小档位(低于则拒绝替换)
--   · ai.lm.recalib_method        —— platt / isotonic
--   · ai.lm.recalib_ece_alert     —— ECE 漂移告警阈值
--
-- 幂等：config_key 已存在则不覆盖(保留面板/运行态调值)。

INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, current_value, value_type, label, description)
SELECT v.* FROM (VALUES
  ('ai.lm.raw_record_enabled',  'ai_quality', 'lm', 'true',  'true',  'bool',   '三头原始分落库',
   'true=sidecar 每 M5 棒把质量/方向/买点头 raw 分写入 hcm_ai.ai_pred_raw（滚动校准数据源）'),
  ('ai.lm.recalib_window_days', 'ai_quality', 'lm', '20',    '20',    'number', '滚动校准窗口(天)',
   'recalibrate_quality.py 仅消费近 N 天 ai_pred_raw 重拟合校准器（避免旧脏数据稀释）'),
  ('ai.lm.recalib_min_samples', 'ai_quality', 'lm', '300',   '300',   'number', '校准最小样本数',
   '单头窗口内样本 < 此值则不重校准（样本不足易过拟合）'),
  ('ai.lm.recalib_min_levels',  'ai_quality', 'lm', '8',     '8',     'number', '校准器最小档位',
   '拟合校准器档位 < 此值即判退化，拒绝替换（保留线上校准器）'),
  ('ai.lm.recalib_method',      'ai_quality', 'lm', 'platt', 'platt', 'string', '校准方法',
   'platt=逻辑回归 logit 校准（默认，与离线层一致）；isotonic=保序分段'),
  ('ai.lm.recalib_ece_alert',   'ai_quality', 'lm', '0.08',  '0.08',  'float',  'ECE 漂移告警阈值',
   '某头期望校准误差(ECE) > 此值 → 写 hcm:ai:quality:calib_alert 告警键')
) AS v(config_key, category, subcategory, default_value, current_value, value_type, label, description)
WHERE NOT EXISTS (
  SELECT 1 FROM hcm_config.metadata m WHERE m.config_key = v.config_key
);

COMMENT ON COLUMN hcm_config.metadata.current_value IS
  '0027 seed: B/C 项质量头校准闭环配置键（raw_record_enabled / recalib_*）';
