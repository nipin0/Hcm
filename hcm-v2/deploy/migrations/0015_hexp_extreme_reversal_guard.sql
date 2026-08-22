-- 0015_hexp_extreme_reversal_guard.sql
-- 极值反转护栏（2026-08-18）：顶/底极值区 + 动量减弱且反向 + 长影线 → 拦原趋势延续单。
-- 幂等：已存在则跳过，不覆盖生产已调优值。
-- 部署：docker cp 到 hcm-v2-postgres-1:/tmp/0015.sql →
--   docker exec -e PGCLIENTENCODING=UTF8 psql -U hcm -d hcm_v2 -v ON_ERROR_STOP=1 -f /tmp/0015.sql
-- Redis 侧需另发 HSET hcm:config:v2 + PUBLISH hcm:config:invalidate（见文件末尾说明），
-- 或由 config_provider 热重载自动回填（删除 Redis 字段后回源 PG）。

INSERT INTO hcm_config.metadata (config_key, current_value, default_value, value_type, category, description)
VALUES
    ('hexp.extreme.reversal_enabled', 'True',  'True',  'bool',   'hexp', '极值反转护栏总开关：顶/底极值+动量减弱反向+长影线→拦原趋势延续单'),
    ('hexp.extreme.wick_min',         '0.60',  '0.60',  'float',  'hexp', '长影线阈值：上/下影占全幅比≥此值视为长影线'),
    ('hexp.extreme.reversal_sl_atr_mult', '0.5', '0.5', 'float',  'hexp', '极值区放行单的 SL 收紧 ATR 倍数（< 常规 sl_atr_mult）')
ON CONFLICT (config_key) DO NOTHING;

-- Redis 双写（如容器内可连 Redis，否则靠 config_provider 热重载回源 PG）：
-- 在 postgres 容器内无法直接 HSET，请在 hcm-v2-redis-1 上执行：
--   HSET hcm:config:v2 hexp.extreme.reversal_enabled True
--   HSET hcm:config:v2 hexp.extreme.wick_min 0.60
--   HSET hcm:config:v2 hexp.extreme.reversal_sl_atr_mult 0.5
--   PUBLISH hcm:config:invalidate hexp.extreme.reversal_enabled
--   PUBLISH hcm:config:invalidate hexp.extreme.wick_min
--   PUBLISH hcm:config:invalidate hexp.extreme.reversal_sl_atr_mult
-- 或最简单：HDEL hcm:config:v2 hexp.extreme.reversal_enabled hexp.extreme.wick_min hexp.extreme.reversal_sl_atr_mult
--   → config_provider 下次读取自动回源 PG 重建合法值。
