-- 0020_hexp_extreme_drift_fix.sql
-- 修复和乘幂(hexp)极值护栏 seed 漂移（2026-08-21）。
-- 根因：PG hcm_config.metadata 中多个极值/动量护栏阈值的 current_value/default_value
-- 与引擎代码 _DEFAULTS 不一致，且漂移成了"更激进/更松"的错误值，导致：
--   - extreme_guard 拦截阈值 mm_retreat_min 被压到 0.02（代码 0.20）：极值高位 BUY 几乎不被拦截 → 追顶
--   - momentum_drain_mm 被压到 0.02（代码 0.15）：高位接刀保护失效
--   - k_extreme 放大到 2.3（代码 1.8）、high_pct 0.8（代码 0.85）、low_pct 0.2（代码 0.15）、
--     wick_min 0.5（代码 0.6）：极值识别更易触发且反转护栏更难触发
-- 综合效果：Donchian 极值高位 + 微动量(≈0.04) 仍被放行追 BUY，配合 reversal_sl_atr_mult=0.5 紧止损
-- → "开在最高点又止损"。与 2026-08-21 的 min_grade drift 同源（PG seed 与代码 _DEFAULTS 不一致）。
--
-- 修复：把这些漂移键对齐回代码 _DEFAULTS（代码为权威真相源）。仅当未做过有意调优时覆盖，
-- 此处 6 个键的 current 值均为明显的漂移错误值，对齐安全。
-- 幂等：用 UPDATE（无则跳过）。
--
-- 部署：docker cp 到 hcm-v2-postgres-1:/tmp/0020.sql →
--   docker exec -e PGCLIENTENCODING=UTF8 psql -U hcm -d hcm_v2 -v ON_ERROR_STOP=1 -f /tmp/0020.sql
-- Redis 侧需 HSET（见末尾），或由 config_provider 热重载自动回源 PG。

UPDATE hcm_config.metadata SET
    default_value = '0.20',
    current_value = CASE WHEN current_value IN ('0.02', '0.2', '0.20') THEN '0.20' ELSE current_value END
WHERE config_key = 'hexp.extreme.mm_retreat_min';

UPDATE hcm_config.metadata SET
    default_value = '1.8',
    current_value = CASE WHEN current_value IN ('2.3', '2.30') THEN '1.8' ELSE current_value END
WHERE config_key = 'hexp.extreme.k_extreme';

UPDATE hcm_config.metadata SET
    default_value = '0.85',
    current_value = CASE WHEN current_value IN ('0.8', '0.80') THEN '0.85' ELSE current_value END
WHERE config_key = 'hexp.extreme.high_pct';

UPDATE hcm_config.metadata SET
    default_value = '0.15',
    current_value = CASE WHEN current_value IN ('0.2', '0.20') THEN '0.15' ELSE current_value END
WHERE config_key = 'hexp.extreme.low_pct';

UPDATE hcm_config.metadata SET
    default_value = '0.6',
    current_value = CASE WHEN current_value IN ('0.5', '0.50') THEN '0.6' ELSE current_value END
WHERE config_key = 'hexp.extreme.wick_min';

UPDATE hcm_config.metadata SET
    default_value = '0.15',
    current_value = CASE WHEN current_value IN ('0.02', '0.2', '0.15') THEN '0.15' ELSE current_value END
WHERE config_key = 'hexp.momentum_drain_mm';

-- Redis 双写（postgres 容器内无法直接 HSET，请在 hcm-v2-redis-1 执行）：
--   HSET hcm:config:v2 hexp.extreme.mm_retreat_min 0.20
--   HSET hcm:config:v2 hexp.extreme.k_extreme 1.8
--   HSET hcm:config:v2 hexp.extreme.high_pct 0.85
--   HSET hcm:config:v2 hexp.extreme.low_pct 0.15
--   HSET hcm:config:v2 hexp.extreme.wick_min 0.6
--   HSET hcm:config:v2 hexp.momentum_drain_mm 0.15
--   PUBLISH hcm:config:invalidate hexp.extreme.mm_retreat_min
--   PUBLISH hcm:config:invalidate hexp.extreme.k_extreme
--   PUBLISH hcm:config:invalidate hexp.extreme.high_pct
--   PUBLISH hcm:config:invalidate hexp.extreme.low_pct
--   PUBLISH hcm:config:invalidate hexp.extreme.wick_min
--   PUBLISH hcm:config:invalidate hexp.momentum_drain_mm
-- 或最简单：删除这些 Redis 字段让 config_provider 下次读取自动回源 PG 重建合法值：
--   HDEL hcm:config:v2 hexp.extreme.mm_retreat_min hexp.extreme.k_extreme hexp.extreme.high_pct \
--         hexp.extreme.low_pct hexp.extreme.wick_min hexp.momentum_drain_mm
