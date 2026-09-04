-- 0012_entry_pending_config.sql
-- 【2026-09-04】入场时机闸门（方案D 挂起）参数配置化 seed（幂等，可重复执行）
--
-- 背景：scheduler.py 中 `_entry_pending_enabled` / `_entry_pending_ttl_sec`
-- 原为 getattr 硬编码默认(True / 900)，从未写入配置中心 → 面板不可见、
-- 无法热调。现将二者纳入 hcm_config.metadata，默认值与改动前行为完全一致，
-- 未 seed 的环境不受影响（代码 fallback 仍为 True / 900）。
--
-- 语义：
--   entry_pending_enabled = false → 关闭挂起机制（退回旧行为：不再等待动量转向）
--   entry_pending_ttl_sec         → 挂起超过该秒数仍未等来动量转向则放弃，不再追

INSERT INTO hcm_config.metadata
    (config_key, category, default_value, current_value, value_type, label, description)
VALUES
    ('signal_tower.entry_pending_enabled', 'signal_tower', 'true', 'true', 'bool',
     '入场挂起总开关',
     'M5 动量与信号方向相反时挂起等待而非市价追单；关闭则退回旧行为'),
    ('signal_tower.entry_pending_ttl_sec', 'signal_tower', '900', '900', 'int',
     '入场挂起超时(秒)',
     '挂起超过此时长仍未等来动量转向则放弃，不再追')
ON CONFLICT (config_key) DO UPDATE SET
    default_value = EXCLUDED.default_value,
    current_value = EXCLUDED.current_value,
    updated_at = now();
