-- 0046_state_min_conf_recalibrated.sql
-- 【数据定标】`state.min_conf` 0.45 → **0.35**（从"手感阈值"改为"验收口径定标值"）。
--
-- ── 为什么改（问题）──────────────────────────────────────────────────
-- 生产 `min_conf = 0.45` 落在 4 分类模型 `max_proba` 分布的**中位附近** ⇒ 实测
-- **51% 的 bar 直接 `low_conf_skip`**（近 6h 36/70 根），而该早退**不参与防抖计数**
-- ⇒ 状态机迁不动（当日实测：连续 8 根 `cls=trend_fade`、其中 7 根低置信）。
-- 该值是"手填"的：按 4 分类 0.25 = 随机水平看，0.45 ≈ 1.8× 随机水平，对**5 seeds
-- bagging 平均**后的概率（会向均值收缩）而言偏高。
--
-- ── 定标依据（回放整链，1923 根洁净窗口，**唯一变量 = 本键**）──────────
--   其余配置固定：require_trigger=1 / dir_tf=M5 / flat_reset=1 / fanout=2 /
--   round_idem=1 / bands=quantile / entry_confirm=1 /
--   触发器仅起点模型分支（use_donchian=0）
--
--   min_conf   漏检   误报     中位提前量   趋势态占比   open
--   0.45        3    16.2%      −2.5        17.6%       19      ← 原值
--   **0.35**   **2**  18.4%    **−1.0**    **22.8%**   **31**   ← 本迁移
--   0.28        2    21.3%      −3.0        25.7%       37
--
--   ⇒ **拐点在 0.35**：漏检 3→2、中位提前量 −2.5→**−1.0**（更早且更准）、
--     出手 19→**31（+63%）**，代价仅误报 +2.2pt。
--   ⇒ 0.28 **被支配**：漏检不再改善，误报再 +2.9pt，提前量反而退化到 −3.0。
--
-- ── 口径说明（为什么用这三个数当判据）────────────────────────────────
--   验收门（方案 §21/§27/§51/§52）判的是"**比滞后指标早几根 bar**"，不是 F1。
--   故以 **漏检 / 误报 / 中位提前量** 三者同时权衡，而不是单看某一项。
--
-- ── 注意（不改变的东西）──────────────────────────────────────────────
--   · 本键只决定"单根判定是否参与防抖"，**不改变**模型本身的判别力；
--   · 误报从 16.2% 升到 18.4% 是**真实代价**，已在迁移内如实记录；
--     Donchian 那类"78% 假起点"的入口仍由 `state.trigger.use_donchian=false` 挡住。
--
-- ── 回滚（一行）─────────────────────────────────────────────────────
--   UPDATE hcm_config.metadata SET current_value='0.45'
--    WHERE config_key='state.min_conf';
--   （同步 Redis L2：HSET hcm:config:v2 state.min_conf 0.45
--     + PUBLISH hcm:config:invalidate state.min_conf）

UPDATE hcm_config.metadata
   SET current_value = '0.35',
       updated_at    = now()
 WHERE config_key = 'state.min_conf';

SELECT config_key, default_value, current_value
  FROM hcm_config.metadata
 WHERE config_key IN ('state.min_conf', 'state.trigger.use_donchian',
                      'state.fsm.flat_reset_enabled', 'state.order_enabled')
 ORDER BY 1;
