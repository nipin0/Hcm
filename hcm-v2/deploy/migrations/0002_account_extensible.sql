-- ─────────────────────────────────────────────────────────────────────────────
-- 0002_account_extensible.sql
-- 账户可扩展性字段：支撑「多终端 / 多机 / 账户启停」的扩展性布局
-- 适用：已通过 init.sql 初始化的现有库（ALTER 增量迁移，向上兼容）
-- 回滚：见文件末尾 --DOWN 段
-- ─────────────────────────────────────────────────────────────────────────────

-- 1) 终端路径（terminal_path）
--    NULL = 系统自动分配终端（用户可选覆盖）。
--    这是「多 MT5 终端 / 多台电脑」部署的绑定键：account_id → terminal_path → 终端实例。
ALTER TABLE hcm_broker.accounts
    ADD COLUMN IF NOT EXISTS terminal_path VARCHAR(512);

COMMENT ON COLUMN hcm_broker.accounts.terminal_path
    IS 'MT5 终端实例路径；NULL=系统自动分配，用户可选覆盖。多终端/多机部署的绑定键（account_id → terminal_path → 终端实例）。';

-- 2) 账户级启停状态（status）
--    running   = 正常参与跟单
--    stopped   = 不参与任何跟单（凭证保留、可恢复）
--    paused    = 暂停（语义同 stopped，预留给「临时挂起」）
--    与 is_active（软删除）正交：删=is_active=false，启停=status。
--    新建账户默认 running，贴合「新增即用」产品承诺。
ALTER TABLE hcm_broker.accounts
    ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'running';

COMMENT ON COLUMN hcm_broker.accounts.status
    IS '账户级启停：running/stopped/paused。与 is_active(软删除)正交；新建默认 running。';

-- 3) 索引：按状态快速筛选「需启用的账户」（bridge 拉起 / 状态广播消费用）
CREATE INDEX IF NOT EXISTS ix_accounts_status
    ON hcm_broker.accounts (status) WHERE is_active = true;

-- 4) 把现有账户统一置为 running（它们此前没有启停概念，默认视为启用）
UPDATE hcm_broker.accounts
   SET status = 'running'
 WHERE status IS NULL OR status = '';

-- ─────────────────────────────────────────────────────────────────────────────
-- --DOWN（如需回滚，按顺序执行）
-- ALTER TABLE hcm_broker.accounts DROP COLUMN IF EXISTS terminal_path;
-- ALTER TABLE hcm_broker.accounts DROP COLUMN IF EXISTS status;
-- DROP INDEX IF EXISTS ix_accounts_status;
-- ─────────────────────────────────────────────────────────────────────────────
