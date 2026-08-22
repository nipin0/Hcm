-- 0011_ai_lm_model_path.sql — AI 评分链路 B9（配置双轨消除）+ B5（校准退化混合）
-- 幂等，可重复执行。
--
-- 背景：
--   quality_scorer.py 是主机 sidecar 进程，此前模型路径只能由命令行传入，而
--   launcher 里硬编码 WITH_MODEL=False → 模型永不加载、ai_score 恒 null；
--   配置中心同时存在 hexp.ai.model_path（容器路径 /app/...，主机进程无法访问）
--   与 ai.lm.model_path（空值）→ 配置双轨且两轨都不通。
--   现将 ai.lm.model_path / ai.lm.calib_path 置为主机绝对路径，sidecar 命令行
--   缺省时回退读取，路径统一由配置中心维护。
--
--   另：calib_final.pkl 由仅 403 条样本拟合，等温回归退化成 {0,0.4,1} 三级阶梯，
--   把 raw prob ∈[0.256,0.615] 全部压成常数 0.40 → ai_score 恒 40.0。新增
--   calib_blend_w / calib_min_levels 两键控制"退化检测 + 与 raw 概率混合"。

INSERT INTO hcm_config.metadata
    (config_key, current_value, default_value, value_type, category, description)
VALUES
    ('ai.lm.model_path',
     'D:/HCM_ASST/hcm-v2/tools/_aiq_artifacts/lgbm_quality_final.txt',
     '', 'string', 'ai',
     'LightGBM 质量模型文件路径（主机 sidecar 绝对路径；留空=不加载模型，ai_score=null 降级）'),
    ('ai.lm.calib_path',
     'D:/HCM_ASST/hcm-v2/tools/_aiq_artifacts/calib_final.pkl',
     '', 'string', 'ai',
     '等温校准器 pkl 路径（留空=不做概率校准，直接用模型原始概率）'),
    ('ai.lm.calib_blend_w',
     '0.5', '0.5', 'float', 'ai',
     '退化校准器与原始概率的混合权重（校准票占比）：0=纯 raw 概率，1=纯校准（恒定常数）'),
    ('ai.lm.calib_min_levels',
     '4', '4', 'int', 'ai',
     '校准器唯一输出档位数低于此值即判定退化，自动启用与 raw 概率混合以恢复分辨率')
ON CONFLICT (config_key) DO UPDATE SET
    current_value = EXCLUDED.current_value,
    default_value = EXCLUDED.default_value,
    value_type    = EXCLUDED.value_type,
    category      = EXCLUDED.category,
    description   = EXCLUDED.description;

-- 废弃键清理：hexp.ai.model_path 是容器内路径，主机 sidecar 无法访问，
-- 且已被 ai.lm.model_path 取代（代码中无读取点）。
DELETE FROM hcm_config.metadata WHERE config_key = 'hexp.ai.model_path';
