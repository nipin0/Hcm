-- 0047_revert_use_donchian_on.sql
--
-- 【目的】撤回 0044（`state.trigger.use_donchian = false`），恢复 Donchian 分支。
--
-- 【为什么撤回 —— 证据来自仓库自带的「验收门」】
--   工具：tools/eval_state_leadtime.py（其 docstring：唯一有意义的验收判据不是 macro F1，
--         而是"同一行情阶段切换事件上，检测器比滞后指标早几根 bar 改判"）
--   口径：XAUUSD M5，标注 40925 bar，走前式 3 折，**生产对齐参数**
--         （--k 2 --min-conf 0.35 --onset-lead 5 --onset-rise-m 3 --onset-onrate 0.1）
--   实测（跨折汇总）：
--     检测器          漏检合计   误报(均)   中位提前量
--     onset_rise        15       12.0%      0.0     ← 仅 rise：会漏 15 次
--     rise|donch         0       21.5%      0.0     ← ★ 三折**零漏检**
--     donchian          14       10.3%     -1.0
--     onset_model      212        5.4%     -3.0
--     model            286       92.8%      1.0
--   工具自身的标定规则（eval_state_leadtime.py:408-409）：
--     「规则：漏检0优先 → 误报低 → 中位早」⇒ **以零漏检优先** ⇒ rise|donch 胜出。
--
-- 【0044 当初的依据为何不成立】
--   0044 依据的是 replay_state_chain.py 的"入场误报 44.9%"（rise 14.1%）。两者口径不同、
--   并不矛盾：验收门问"会不会在震荡期乱报"（donchian 10.3%，合格），回放问"报出来的突破
--   有多大概率是标签定义的起点"（约 55%）。前者决定"触发器是否可用"，后者决定"入场精度"，
--   且后者可由方向模块/策略层过滤，不应在触发器层一刀切掉。
--
-- 【关闭它的直接后果（本次迭代目标冲突）】
--   关掉后趋势入口只剩 rise。而生产 `state.trigger.rise_thr = 0.2948`，
--   与"按目标触发率分位标定"的口径不自洽 ⇒ 全历史 trigger_on 仅 1/363 = **0.28%**
--   ⇒ 铁律要求的三条策略路径中「趋势初生 / 趋势中段」事实上**不可触发**，
--   与目标「及时判断行情使用对应交易策略」直接冲突。
--
-- 【回滚】把 true 改回 false 即可（本迁移只改一个键）。
--
-- 依据文件：tools/eval_state_leadtime.py, deploy/migrations/0044_state_trigger_drop_donchian.sql

UPDATE hcm_config.metadata
   SET current_value = 'true',
       updated_at    = now()
 WHERE config_key = 'state.trigger.use_donchian';

-- 同步 Redis L2（L2 旧值会挡住 PG，本仓库已两次踩坑）
--   redis-cli HSET hcm:config:v2 state.trigger.use_donchian true
--   redis-cli PUBLISH hcm:config:invalidate state.trigger.use_donchian
