-- 0013_ai_deepseek_features.sql
-- 【DeepSeek 赋能 LightGBM 新模型 · 设计文档阶段 1.2/1.3/1.4】
-- 将 DeepSeek 异步票(fake_prob/ai_sl_coeff/continuity_score)从「仅推理期融合」
-- 升级为「训练特征增强 + 标签校准 + 推理融合调参」三位一体。
--
-- 本迁移仅做配置 seed（幂等，可重复执行），不改动任何代码逻辑：
--   1. ai.fuse.* 融合权重双写，杜绝 Redis 丢失漂移（设计文档 1.4 调参基线）
--   2. 记录 ds 特征已固化进 FEATURE_COLS（代码侧在 quality_scorer.py / quality_features.py 已落地）
--
-- 执行（在 hcm-v2-postgres-1 容器内）：
--   docker cp 0013_ai_deepseek_features.sql hcm-v2-postgres-1:/tmp/
--   docker exec -e PGCLIENTENCODING=UTF8 hcm-v2-postgres-1 \
--     psql -U hcm -d hcm_v2 -v ON_ERROR_STOP=1 -f /tmp/0013_ai_deepseek_features.sql
-- 随后 Redis 侧 HSET（见文件末尾注释，经 config_provider 双写或 redis-cli）。

INSERT INTO hcm_config.metadata (config_key, current_value, default_value, value_type, category, description)
VALUES
    ('ai.fuse.w_lm',          '0.6', '0.6', 'float', 'ai', '融合权重：LightGBM 本地票权重（设计文档 1.4，和不为1时归一化）'),
    ('ai.fuse.w_ds',          '0.4', '0.4', 'float', 'ai', '融合权重：DeepSeek 异步票权重（新模型训练已吸收 ds 特征后，可逐步下调此值让模型内化）'),
    ('ai.fuse.ds_max_age_sec','900', '900', 'float', 'ai', 'DeepSeek 票最大有效年龄(秒)，超时丢弃退化为 lm_only（分钟级异步刷新，行情走远不参裁）')
ON CONFLICT (config_key) DO UPDATE
    SET default_value = EXCLUDED.default_value,
        value_type    = EXCLUDED.value_type,
        category      = EXCLUDED.category,
        description   = EXCLUDED.description
    WHERE hcm_config.metadata.current_value IS NULL;  -- 已手动调过的键不覆盖

-- 注意: ds 三特征(ds_fake_prob/ds_sl_coeff/ds_continuity)本身不需要配置键，
-- 它们已由 quality_scorer.py 的 FEATURE_COLS + build_features 注入，并在
-- quality_features.py 从 hcm_ai.ds_output 表近邻匹配落库（缺省 0.0，与推理侧同构）。
-- 重训新模型(train_signal_quality.py)后，这些列会进入模型特征名（向后兼容旧模型：
-- score_one 用 model.feature_name() 自行挑选，旧模型忽略多余列，ai_score 输出不变）。
