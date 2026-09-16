-- 0036_state_trigger_required_on.sql
-- 【2026-09-15】开启"趋势态入口**必须**由起点触发器确认"（核心机制变更 · 变更说明）
--
-- 依据：docs/设计方案_信号塔状态机与交易策略重构_20260914.md §55（item 1 评估）
-- 消费者：signal_tower/state_machine.py 的 `decide()` 第 8.5 段（趋势态入口的两道门）
--
-- ── 为什么可以开（灰度风险 = 0 的论证）──────────────────────────────
--   `state.order_enabled = false`（本迁移前实测确认）⇒ 状态机**只产意图、不下单**。
--   故本变更只影响**观测量**（`hcm_signal.market_state_log` + Redis 上屏 + 指令键），
--   不改变任何真实委托。回滚 = 把 current_value 改回 false（一个键，30s 热重载生效）。
--
-- ── 实测依据（tools/replay_state_chain.py，A/B 唯一变量 = 本键）────────
--   回放调用**线上同一** MarketStateMachine / StateStrategy / trend_trigger /
--   trend_direction，洁净窗口 2026-09-07..09-15（1920 根 M5，模型 v3 实盘同款）：
--
--     指标（验收口径=真值"趋势段上升沿"）   false(旧)     true(新)
--     ─────────────────────────────────────────────────────────
--     漏检                                   0            0
--     误报                                44.2%        34.6%   ← −9.6pt
--     中位提前量                          +0.0        −2.0     ← 从"不提前"到"提前 2 根"
--     提前占比                            47.1%        58.8%
--     趋势态占用                          46.7%        38.1%
--     →S2/S3 入口（**受本键约束**）          33           31
--       其中触发器未响                      10            0
--     非法迁移（I1）                         1            0
--     不变式 I2..I9                          0            0
--
-- ── 必须诚实标注的局限 ────────────────────────────────────────────
--   1. **作用面只有 →S2/S3 入口**：进入 S4（趋势衰竭）的入口**不受本键约束**
--      （S4 不下单，故对交易无害），实测 87→91 次。这正是"误报仍 34.6%"、
--      且远高于 donchian 基线 10.1% 的原因 —— 大部分"趋势态"其实是 S4 停留。
--   2. 样本小：单品种 / 单周期 / 单窗口（1920 根 / 17 个真值事件）。
--   3. 回放的模拟 R **不是业绩**（无点差/滑点/冷却，单持仓，按 bar 收盘成交），
--      故本迁移的**依据是验收口径（漏检/误报/提前量）而非 R**。
--
-- ── 纪律 ─────────────────────────────────────────────────────────
--   `default_value` 保持 'false' = 代码内置的**保守默认**（改代码默认值属另一类变更）；
--   `current_value` = 'true' = 本次**生产决策**。二者语义分开，回滚只动 current_value。

INSERT INTO hcm_config.metadata
    (config_key, category, default_value, current_value, value_type, label, description)
VALUES
    ('state.trigger.required', 'state', 'false', 'true', 'bool',
     '趋势态入口须触发器确认',
     '开启后 S2/S3 的进入必须同时满足：起点触发器响 且 方向≠NONE。'
     'A/B 实测（1920 根洁净 M5，唯一变量）：误报 44.2%→34.6%、中位提前量 +0.0→−2.0、'
     '漏检保持 0、非法迁移 1→0。'
     '⚠ 局限：只约束 →S2/S3 入口，**进入 S4 的入口不受约束**（S4 不下单故无害）；'
     '样本为单品种单窗口。'
     '回滚：current_value 置回 false（30s 热重载生效）')
ON CONFLICT (config_key) DO UPDATE SET
    current_value = EXCLUDED.current_value,
    updated_at = now();

-- ── 迁移后自检（必须看到 true）────────────────────────────────────
-- SELECT config_key, default_value, current_value
--   FROM hcm_config.metadata WHERE config_key = 'state.trigger.required';
