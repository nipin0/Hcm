-- 0017_hexp_zone_hardgate.sql
-- 方案 B (2026-08-19) zone 硬闸门配置 seed
-- 把「方向逆着结构位开仓」挡在门外，与方案 A (hexp.extreme.* Donchian 极值反转陷阱) 互补、串联。
-- 幂等：已存在则跳过（不覆盖线上热改值）。
-- 真源 = PG hcm_config.metadata；Redis hcm:config:v2 由 config_provider 在读取时回源或经 config_provider.set 双写。

INSERT INTO hcm_config.metadata (config_key, current_value, default_value, value_type, category, description, updated_at)
VALUES
  ('hexp.zone.hard_block_enabled', 'false', 'false', 'bool', 'hexp',
   '方案B zone 硬闸门总开关：开启后现价显著越过方向对齐结构位的单(NO_TRADE)挡在门外。默认关。', now()),
  ('hexp.zone.hard_block_atr_mult', '0.3', '0.3', 'number', 'hexp',
   '判定逆结构位的 ATR 倍数容差：价格偏离结构位 > 此倍数×ATR 才算逆结构被封。越小越严。', now())
ON CONFLICT (config_key) DO NOTHING;
