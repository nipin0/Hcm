-- 0043_state_fsm_flat_reset_on.sql
-- 【根因修复】把 `state.fsm.flat_reset_enabled` 置 true。
--
-- ── 问题（实测证据）────────────────────────────────────────────────
-- 生产 `state.fsm.flat_reset_enabled = false`（**模块默认值，自始未开启**）。
-- 该开关的既有语义（`state_machine.decide` 规则 6 / 模块 docstring）：
--     趋势态 + 无持仓  →  **下一根立即复位 S0_IDLE**（"持仓归零复位"）
-- 其存在目的（`tools/verify_state_machine_age.py` 原注释原话）：
--     "防止卡在**无持仓的趋势态**"
-- 关闭它的后果（2026-09-15 生产实测）：
--     模型 fade 先验 68.6% + `state.min_conf=0.45` 使 51% 的 bar 直接
--     `low_conf_skip`（**不参与防抖计数**）⇒ 迁出 S4 需要 `k_fade=2` 根
--     **连续且 decided** 的非 fade 判定，实际极难达成 ⇒ FSM **长期滞留
--     S4_TREND_FADE**（obs 表实测：07:00→12:20 **连续 54 根**，近 6h 占 77%）
--     ⇒ **S4 = 禁新开** ⇒ 长时间零下单。
--
-- ── 因果实验（tools/replay_state_chain.py，唯一变量 = 本开关）─────────
--   洁净窗口 1923 根，其余配置完全相同（require_trigger=1 / dir_tf=M5 /
--   bands=quantile / entry_confirm=1）：
--       指标             FR0(false，生产真值)   FR1(true，设计意图)
--       趋势态入口                1                    130
--       →S2/S3 入口               1                     30
--       open 意图（8 天）        22                     72
--       非法迁移 I1               1                      0
--       链路验证                1 项失败              全过
--   ⇒ 生产语义下 open 少 3.3 倍、趋势态入口少 130 倍；期望出单间隔被拉长到
--     ≈7 小时（22 单/8 天）⇒ 该窗口内"零下单"是**必然**，而非偶发。
--
-- ── 为什么这是缺陷而非策略选择 ────────────────────────────────────
--   该开关的启用条件在模块里写明是"**策略层已接线**"。本系统 `state.order_enabled`
--   已为 true（策略层已接线）⇒ 按设计它**本应开启**。
--   另：关闭它还会产生 1 次**非法状态迁移**（FR1 无）—— 说明该路径本身不自洽。
--
-- ── ⚠ 同时纠正评估工具（伪交付根因）────────────────────────────────
--   `tools/replay_state_chain.py` 此前**硬编码** `fsm._flat_reset = True`，
--   而生产是 false ⇒ **回放与生产的 FSM 语义不同**，历史上所有"回放显示每天
--   ~9 单"的结论对生产无效。现已改为 `--flat-reset`（**默认 0 = 生产真值**），
--   使回放忠实复现生产。
--
-- ── 回滚 ──────────────────────────────────────────────────────────
--   一条 SQL 即可： UPDATE hcm_config.metadata
--                   SET current_value='false'
--                 WHERE config_key='state.fsm.flat_reset_enabled';
--   并同步 Redis L2（否则 L2 命中旧值挡住 PG）：
--     HSET hcm:config:v2 state.fsm.flat_reset_enabled false
--     PUBLISH hcm:config:invalidate state.fsm.flat_reset_enabled

UPDATE hcm_config.metadata
   SET current_value = 'true',
       updated_at    = now()
 WHERE config_key = 'state.fsm.flat_reset_enabled';

-- 自证：迁移后必须为 true（default_value 保持 false 不动：默认值代表"未明确配置"，
-- 而本次是**显式放行**，故只改 current_value —— 与 0036 同一原则）
SELECT config_key, default_value, current_value
  FROM hcm_config.metadata
 WHERE config_key IN ('state.fsm.flat_reset_enabled', 'state.order_enabled')
 ORDER BY 1;
