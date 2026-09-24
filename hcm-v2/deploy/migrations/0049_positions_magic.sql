-- ============================================================================
-- 0049_positions_magic.sql
--
-- 目的：为风控「同向保本闸门」提供**持仓的 magic**，使"保本后追单"的判定维度
--       由 (账户, 方向) 细化为 (账户, 方向, magic 族)。
--
-- 需求（用户 2026-09-16/17 拍板）：
--   持仓单保本后，有信号要下新单时，先判断是否"同类型信号（同 Magic 族）"；
--   只有同族之间才构成"保本 → 可追单"关系；同族订单照常受本闸门约束；
--   不同族互不牵连；无同族持仓 → 直接放行。
--   族粒度 = **前导逻辑码**：11=hexp / 12=scoring / 21=live_override /
--   55=range / 61=state_osc / 62=state_trend。
--
-- 写方：桥
--   · mt5_bridge.place_mt5_order 返回值带 magic → 开仓 INSERT 落库；
--   · position_sync._pg_update_position 每轮同步 `magic = pos.magic`（MT5 真值）
--     → 存量持仓自动回填（无需单独回填脚本）。
-- 读方：hcm-risk-engine rule_chain._check_cooldown（**族公式的唯一实现点**在风控侧）。
--
-- 说明：本列存**原始 magic**（含 FSM 8 位 LL·SS·RR·TT 布局），不做归族存储——
--       归族只在风控侧算一次，避免同一规则两份实现（铁律第十三章）。
-- 幂等：可重复执行。
-- ============================================================================

ALTER TABLE hcm_trading.positions ADD COLUMN IF NOT EXISTS magic BIGINT;

COMMENT ON COLUMN hcm_trading.positions.magic IS
    'MT5 订单 magic 原值（桥从 MT5 持仓对象直写，含 FSM 8 位 LL·SS·RR·TT 布局）。'
    'NULL = 历史行/未知。风控按前导逻辑码归族：11=hexp 12=scoring 21=live_override '
    '55=range 61=state_osc 62=state_trend。';
