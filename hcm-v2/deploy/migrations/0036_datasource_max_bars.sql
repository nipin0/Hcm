-- 0036_datasource_max_bars.sql
-- 【2026-09-15 配置真源补齐】datasource.max_bars 此前**只存在于 Redis** hot 层，
-- PG hcm_config.metadata 里**没有这一行** —— 违反"PG 是唯一 SoT、Redis 只是热缓存"的既定纪律，
-- 后果是 Redis 一旦被清（本项目发生过：容器重建导致 hcm:config:v2 全空），该键会**静默回退
-- 到代码默认值 500**，桥启动全量载入量随之缩水 4 倍，行情覆盖深度悄悄变浅且无告警。
--
-- 本次操作背景：为执行 §36 的历史污染行清洗，临时把该键提到 66000 让桥启动时
-- 按经纪商权威数据**全量覆盖重写**（写入语义已于 2026-09-14 改为覆盖式，故一次重拉即自愈）。
-- 清洗完成后**恢复为 2000**（正常运行值）—— 66000 会让每次桥重启多耗约 8 分钟阻塞启动，
-- 不适合作为常驻配置。
--
-- 数值依据：2000 根是既有生产值（2026-09-15 13:33 前 Redis 实测值），此处补记为 SoT。

INSERT INTO hcm_config.metadata
    (config_key, category, default_value, current_value, value_type, label, description)
VALUES
    ('datasource.max_bars', 'datasource', '2000', '2000', 'number',
     '桥启动全量载入根数',
     '桥启动时每周期经 copy_rates_from_pos 拉取并（覆盖式）写入 PG 的 K 线根数。'
     '2000 为正常运行值；历史污染清洗等一次性作业可临时提高（如 66000），'
     '但每次重启的阻塞时间与它成正比（实测约 469 行/秒），作业后须恢复')
ON CONFLICT (config_key) DO UPDATE SET
    default_value = EXCLUDED.default_value,
    current_value = EXCLUDED.current_value,
    updated_at = now();
