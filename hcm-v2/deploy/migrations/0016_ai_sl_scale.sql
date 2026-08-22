-- 0016_ai_sl_scale.sql
-- LightGBM ai_score → SL 宽度缩放（2026-08-18）。
-- 期望：AI 分的高低缩放 SL 宽度，数值范围参考设计文档 hcm-ai-quality-scorer-design.md
--       ai_sl_coeff 区间 0.8~1.5×ATR。方向=分高→宽(1.5)、分低→窄(0.8)。
--       AI 断联/失效 → 回退会话 SL；DeepSeek 不再直接干预订单 SL/TP（仅辅助校准 LightGBM）。
-- 幂等：已存在则跳过，不覆盖生产已调优值。
-- 部署：docker cp 到 hcm-v2-postgres-1:/tmp/0016.sql →
--   docker exec -e PGCLIENTENCODING=UTF8 psql -U hcm -d hcm_v2 -v ON_ERROR_STOP=1 -f /tmp/0016.sql
-- Redis 侧：HDEL hcm:config:v2 ai.lm.sl_scale_enabled ai.lm.sl_scale_min ai.lm.sl_scale_max
--   → config_provider 下次读取自动回源 PG 重建合法值；或逐键 HSET + PUBLISH hcm:config:invalidate。

INSERT INTO hcm_config.metadata (config_key, current_value, default_value, value_type, category, description)
VALUES
    ('ai.lm.sl_scale_enabled', 'True', 'True', 'bool',   'ai_quality', 'LightGBM ai_score 缩放 SL 宽度总开关：true=AI 有效时用 0.8~1.5×ATR 替代会话 SL；AI 断联/失效回退会话 SL'),
    ('ai.lm.sl_scale_min',     '0.8',  '0.8',  'number', 'ai_quality', 'AI 分缩放 SL 系数下限（×ATR）：ai_score 最低(0)时的 SL 距离'),
    ('ai.lm.sl_scale_max',     '1.5',  '1.5',  'number', 'ai_quality', 'AI 分缩放 SL 系数上限（×ATR）：ai_score 最高(100)时的 SL 距离')
ON CONFLICT (config_key) DO NOTHING;

-- Redis 双写（在 hcm-v2-redis-1 上执行，或 HDEL 让 config_provider 回源 PG）：
--   HSET hcm:config:v2 ai.lm.sl_scale_enabled True
--   HSET hcm:config:v2 ai.lm.sl_scale_min 0.8
--   HSET hcm:config:v2 ai.lm.sl_scale_max 1.5
--   PUBLISH hcm:config:invalidate ai.lm.sl_scale_enabled
--   PUBLISH hcm:config:invalidate ai.lm.sl_scale_min
--   PUBLISH hcm:config:invalidate ai.lm.sl_scale_max
