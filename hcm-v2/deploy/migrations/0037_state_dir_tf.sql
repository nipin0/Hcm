-- 0037_state_dir_tf.sql
-- 【2026-09-15】方向**来源周期**：M5 → H1（item 2，方案 §56）
--
-- 消费者：signal_tower/scheduler.py 的 `_load_trigger_config` → `_produce_signal`
--         的"方向来源周期"分支（`state.dir.tf`），跨周期对齐用
--         `trend_direction.align_last_closed`（线上/离线回放/离线标定**同一实现**）。
--
-- ── 为什么必须换（这不是调参，是**符号正确性**问题）──────────────────
-- `tools/eval_trend_direction.py --sweep` 在洁净数据上跑 **21 组 (thr,k) 全网格**：
--
--   方向来源   双侧合计边际 both_edge           结论
--   ─────────────────────────────────────────────────────────────
--   M5        **21/21 组全部为负**（−0.017..−0.034）  DOWN 侧符号**反向** ⇒ 不可用
--   H1        **21/21 组全部为正**（+0.008..+0.030）  符号正确 ⇒ 可用
--
--   （M5 的 DOWN 组 `dn_edge` 全为正 = "判 DOWN 之后上涨占比反而更高"；
--     §20/§51.7 早有此结论，本次在全网格上复现，排除单点偶然。）
--
-- ── 端到端 A/B（tools/replay_state_chain.py，洁净窗口 1922 根 M5，模型 v3）──
--   唯一变量 = 本键；其余与生产一致（`require_trigger=true` 见 0036）：
--
--     指标（验收口径=真值"趋势段上升沿"）     dir=M5    dir=H1
--     ─────────────────────────────────────────────────────────
--     漏检                                      0         0
--     误报                                   34.6%     28.8%   ← 改善
--     中位提前量                             −2.0      +0.0     ← **变差（不再提前）**
--     趋势态占用                             38.1%     26.3%
--     →S2/S3 入口（受触发器约束）              31        15
--     trigger_enter(up/down)                12/19      1/14
--
-- ── 取舍（必须讲清，不能只报好消息）────────────────────────────────
--   换 H1 的代价是**提前量**：中位从"提前 2 根"退回"不提前"。原因是方向门变慢
--   （大周期确认更晚），而 `require_trigger=true` 使方向成为**必需项**而非仅否决。
--   本迁移仍选 H1，理由：**方向先要正确，再谈快慢** —— 用一个符号反向的方向做
--   必需门，是会让系统**系统性地偏向错误一侧**的潜在缺陷，其"提前量更好"很可能
--   来自噪声（门更松 → 入口更多 → 更早撞上真值，代价是误报高 5.8pt）。
--   提前量应通过**触发器**侧去找回（§30.4-2 已列为下一步），而不是靠放开方向门。
--
-- ── 纪律 ─────────────────────────────────────────────────────────
--   `default_value` 保持 'M5'（= 代码内置行为，改代码默认属另一类变更）；
--   `current_value` = 'H1' = 本次生产决策。回滚只动 current_value（30s 热重载生效）。
--   ⚠ 该键是**字符串**键，不在 `_load_trigger_config` 的 float 分支里，单独读取。

INSERT INTO hcm_config.metadata
    (config_key, category, default_value, current_value, value_type, label, description)
VALUES
    ('state.dir.tf', 'state', 'M5', 'H1', 'string',
     '方向来源周期',
     '用哪个周期判趋势方向（"大周期定方向、小周期定入场"）。'
     '全网格实测：M5 的 DOWN 侧符号反向（both_edge 21/21 组为负，不可用）；'
     'H1 符号正确（21/21 组为正）。'
     '端到端 A/B：误报 34.6%→28.8%、但中位提前量 −2.0→+0.0（取舍已记录在方案 §56）。'
     '跨周期对齐由 trend_direction.align_last_closed 保证前视闭合（三处共用）。'
     '回滚：current_value 置回 M5')
ON CONFLICT (config_key) DO UPDATE SET
    current_value = EXCLUDED.current_value,
    updated_at = now();

-- ── 迁移后自检（必须看到 current=H1）──────────────────────────────
-- SELECT config_key, default_value, current_value
--   FROM hcm_config.metadata WHERE config_key = 'state.dir.tf';
--
-- ⚠ **本迁移写 PG 不足够**：`ConfigProviderV3` 解析顺序是 L1(内存) → L2(Redis hcm:config:v2)
--   → L3(PG)。**Redis 命中即返回**，故若 L2 已有旧值，PG 改了也不生效（实测踩到）。
--   规范做法是用 `ConfigProviderV3.set()`（PG → Redis → PUBLISH 失效）；手工执行时须：
--     redis-cli HSET hcm:config:v2 state.dir.tf H1
--     redis-cli PUBLISH hcm:config:invalidate state.dir.tf
