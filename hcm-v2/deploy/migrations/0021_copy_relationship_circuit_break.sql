-- 0021_copy_relationship_circuit_break.sql
-- 2026-08-25 修复：按账户跟单熔断依赖 hcm_copy.relationships 缺失列。
--
-- 根因：8/25 将跟单每日盈亏熔断从全局 close.follow_* 迁移为按账户读取
--   hcm_copy.relationships 的 max_daily_profit / circuit_break_enabled（max_daily_loss 已存在），
--   但 deploy/migrations 仅至 0020、init.sql 也未定义这两列 → 存量/全新库查询均报
--   "column does not exist" → copy API 增改查硬失败、桥按账户熔断静默回退全局旧值。
--
-- 修复：补两列（幂等 ADD COLUMN IF NOT EXISTS）。
--   - max_daily_profit DECIMAL(10,2) DEFAULT 0        （占 equity% 上限；0=不限制盈利方向）
--   - circuit_break_enabled BOOLEAN NOT NULL DEFAULT TRUE（熔断默认开启；阈值 0=不触发）
-- 存量行：circuit_break_enabled 自动取 TRUE（机制就位）；profit/loss 默认 0（不限制，行为不变）。
-- 阈值由前端/API 按关系设置；未设置前不触发熔断，无意外行为变化。
--
-- 部署（需显式授权后执行，属生产 schema 写入）：
--   docker cp 0021_copy_relationship_circuit_break.sql hcm-v2-postgres-1:/tmp/ && \
--   docker exec -e PGCLIENTENCODING=UTF8 psql -U hcm -d hcm_v2 -v ON_ERROR_STOP=1 -f /tmp/0021.sql
--
-- 可选回填：将 running 关系按全局旧值 30(盈利%)/50(亏损%) 武装熔断（取消注释前请确认阈值意图）。
-- UPDATE hcm_copy.relationships
--     SET max_daily_profit = 30, max_daily_loss = 50, circuit_break_enabled = TRUE
-- WHERE status = 'running';

ALTER TABLE hcm_copy.relationships
    ADD COLUMN IF NOT EXISTS max_daily_profit DECIMAL(10,2) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS circuit_break_enabled BOOLEAN NOT NULL DEFAULT TRUE;
