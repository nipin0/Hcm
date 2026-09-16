-- 0044_state_trigger_drop_donchian.sql
-- 【贴合行情】关闭触发器的 Donchian 分支（`state.trigger.use_donchian=false`），
-- 只保留"起点模型（rise）"分支。
--
-- ── 依据（回放整链，1923 根洁净窗口，唯一变量=触发器分支）────────────
--   配置                     进入次数   误报     漏检   中位提前量   open
--   rise + donch（生产现值）     43     48.7%     0      −3.0       125
--   **仅 rise（本迁移）**         2    **14.1%**   0    **−2.0**      19
--   仅 donch                     42     44.9%     0      −1.0       125
--   ⇒ Donchian 分支贡献 **42/43** 次进入，却把误报从 14.1% 抬到 48.7%：
--     它是"**不贴合行情**"假起点的来源（占其进入的 ~78%）。
--   ⇒ 起点模型分支：误报 **14.1%**（与验收门最优基线 donchian 的 10.1% 同级）、
--     **漏检 0**、中位提前量 **−2.0**（比 −3.0 更准）。
--
-- ── 代价（如实记录）────────────────────────────────────────────────
--   open 意图 125 → 19（8 天）≈ **2.4 单/天**。这是"以量换贴合度"的**有意选择**：
--   目标的"下单"是**贴合行情**的单，而非把假起点也算成进展。
--   ⚠ 若后续要恢复单量，正确做法是**提升 Donchian 分支的质量**（例如要求它与
--     方向模块的大周期方向一致、或加回调确认），而**不是**直接把它打开 ——
--     直接打开只会把误报推回 ~49%。
--
-- ── 前置（本迁移单独不足以解决"不下单"）──────────────────────────
--   必须同时完成**代码部署**（重启 signal-tower）：
--     ① `state_machine.py`：触发器入口移到 `low_conf_skip` 早退**之前**
--        （否则 51% 的 bar 上触发器连看都不被看一眼，本迁移的效果无从体现）；
--     ② `state_machine.py`：`flat_reset` / 震荡锁止同样移出置信闸；
--     ③ `scheduler.py`：`valid=False` 不再映射成 `"none"`（契约修复）。
--
-- ── 回滚 ──────────────────────────────────────────────────────────
--   UPDATE hcm_config.metadata SET current_value='true'
--    WHERE config_key='state.trigger.use_donchian';
--   （并同步 Redis L2：HSET hcm:config:v2 state.trigger.use_donchian true
--     + PUBLISH hcm:config:invalidate state.trigger.use_donchian）

UPDATE hcm_config.metadata
   SET current_value = 'false',
       updated_at    = now()
 WHERE config_key = 'state.trigger.use_donchian';

SELECT config_key, default_value, current_value
  FROM hcm_config.metadata
 WHERE config_key IN ('state.trigger.use_donchian', 'state.trigger.use_rise',
                      'state.trigger.required', 'state.order_enabled')
 ORDER BY 1;
