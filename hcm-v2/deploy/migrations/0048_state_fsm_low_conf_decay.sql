-- 0048_state_fsm_low_conf_decay.sql
-- 【根因修复】把 `state.fsm.low_conf_policy` 由 hold（既有默认）置为 decay。
--
-- ⚠ 本迁移**尚未 apply**。DB 状态变更请按正常迁移流程执行（同 0034/0043 先例）。
--   代码侧已实现（`state_machine.py` 第 4c 步 + DEFAULTS），本文件只负责把
--   **运行时真值**切过去 —— 不 apply 时行为与现状**逐位一致**（模块默认 hold）。
--
-- ── 问题（实测证据）────────────────────────────────────────────────
-- 1) 本键**在 PG 与 Redis 中均不存在**（2026-09-16 实测）⇒ 塔一直使用模块默认
--    `hold`。而 `hold` 的语义是：**低置信 bar 不参与类别防抖（保持前值）**。
-- 2) 病根链条（本迭代主线"解决指标滞后"）：
--      模型在"行情要变"的时刻恰恰**不确定** ⇒ `decided=False` ⇒ hold 语义下
--      **继续持有旧结论** ⇒ 趋势 ON 难熄灭（需连续 k_exit 根**且 decided** 的非趋势）
--      ⇒ 趋势态长期挂着 ⇒ 表现为**滞后 + 高误报**。
--    同一病根在生产已见三处后果：触发器入口被 `low_conf_skip` 吃掉、
--    `flat_reset` 被架空（0034/0043 已修）、S4 滞留占 77%。
--
-- ── 因果实验（唯一变量 = 本策略；`tools/replay_state_chain.py --low-conf`）──
--   同窗口 2173 根 / flat_reset=1 / require_trigger=false / 同一模型目录：
--       指标                  hold（生产现值）      decay（本迁移）
--       漏检                       0                  0
--       中位提前量               −1.0               −7.0（更早）
--       **误报**               **48.1%**          **37.8%**（−10.3pt）
--       趋势态占比               50.5%              38.9%（−11.6pt）
--       open 意图                 144                104（−28%）
--       加仓笔数                    8                  3
--       不变式 I1–I10          全过 ✓              全过 ✓
--   验收门（`tools/eval_state_leadtime.py`，40925 标注 bar / 走前式 3 折）：
--       model(hold) 漏检 475 / 误报 95.6% / 中位 **+5.0（滞后）**
--       model(decay) 漏检   4 / 误报 51.5% / 中位 **−1.0（提前）**
--   ⇒ 两个口径**方向一致**：decay 同时降低误报并把滞后转为提前。
--
-- ── ⚠ 统计诚实性（不得省略）────────────────────────────────────────
--   回放的"真值事件数"仅 **17** ⇒ 中位 −1.0 → −7.0 的差异**不构成统计显著**，
--   本迁移的依据是"四项方向性指标一致更优 + 不变式全过"，不是某个点的精确值。
--   上线后应以**生产误报/提前量**复核；若误报未降，优先回滚本键（一条 SQL）。
--
-- ── ⚠ 需要重启信号塔 ──────────────────────────────────────────────
--   运行中的塔进程为**旧代码**（其启动早于 `low_conf_policy` 引入：日志中无
--   `MarketStateMachine config loaded | ... low_conf_policy=...` 行）。
--   Python 不热重载模块 ⇒ 仅写配置**不会**让 decay 生效，必须重启塔容器。
--   （桥不必重启：本改动不涉及桥加载的任何文件。）
--
-- ── 回滚 ──────────────────────────────────────────────────────────
--   一条 SQL 即可：
--     UPDATE hcm_config.metadata SET current_value='hold', updated_at=now()
--      WHERE config_key='state.fsm.low_conf_policy';
--   并同步 Redis L2（否则 L2 命中旧值挡住 PG）：
--     HSET hcm:config:v2 state.fsm.low_conf_policy hold
--     PUBLISH hcm:config:invalidate state.fsm.low_conf_policy
--
-- ── 纪律 ──────────────────────────────────────────────────────────
--   `default_value` 保持 `hold` 不动：默认值代表"未明确配置"（保守），本次是
--   **显式启用**，故只写 `current_value` —— 与 0036/0043 同一原则。
--   ⚠ 本键此前**不存在**于 metadata ⇒ 必须 INSERT ... ON CONFLICT（纯 UPDATE 会静默 no-op）。

INSERT INTO hcm_config.metadata
    (config_key, default_value, current_value, value_type, description, updated_at)
VALUES
    ('state.fsm.low_conf_policy', 'hold', 'decay', 'text',
     '低置信 bar 的处理语义：hold=保持前值(既有) | decay=视为震荡并参与防抖（2026-09-16 由 hold 切 decay）',
     now())
ON CONFLICT (config_key) DO UPDATE
    SET current_value = 'decay',
        updated_at    = now();

-- 自证：迁移后 current_value 必须为 decay，default_value 必须仍为 hold
SELECT config_key, default_value, current_value
  FROM hcm_config.metadata
 WHERE config_key = 'state.fsm.low_conf_policy';

-- ⚠ 写 PG 不足够：`ConfigProviderV3` 是 L1(内存) → **L2(Redis hcm:config:v2)** → L3(PG)，
--   **Redis 命中即返回**。必须一并执行（与 0034/0039/0040/0043 同一纪律）：
--     redis-cli HSET hcm:config:v2 state.fsm.low_conf_policy decay
--     redis-cli PUBLISH hcm:config:invalidate state.fsm.low_conf_policy
--   然后重启信号塔（旧进程为旧代码，见上）：
--     docker restart hcm-v2-hcm-signal-tower-1
