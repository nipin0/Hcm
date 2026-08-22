-- 0009: hexp 极值追单收紧 SL 距离（C 方案）配置键 seed
-- 与 A+B（动量感知极值闸门 + 回踩支撑位诊断，见 0008）配套。
-- extreme_chase=True 时，scheduler 把 SL 的 ATR 倍数 × chase_sl_mult（默认 0.7），
-- 显式写入信号 sl_price/tp1，桥直接采用（非零 sl_price），TP 不变 → R:R 改善。
-- 幂等：current_value 为空才 seed，避免覆盖面板已调值。

INSERT INTO hcm_config.metadata (config_key, current_value, default_value, value_type, category, description)
VALUES
    ('hexp.extreme.chase_sl_mult', '0.7', '0.7', 'float', 'hexp',
     '极值追单(extreme_chase)时 SL 的 ATR 倍数缩放系数，默认 0.7=收紧 30% 距离；设 1.0 即关闭收紧')
ON CONFLICT (config_key) DO UPDATE
    SET default_value = EXCLUDED.default_value,
        value_type    = EXCLUDED.value_type,
        category      = EXCLUDED.category,
        description   = EXCLUDED.description
    WHERE hcm_config.metadata.current_value IS NULL;

-- 仅当 Redis 缺失时由 config_provider 回源 PG 自动补；此处主动 HSET 确保立即生效。
-- 注意：信号塔读 hexp.extreme.* 经 config_provider（PG + Redis hcm:config:v2 缓存）。
