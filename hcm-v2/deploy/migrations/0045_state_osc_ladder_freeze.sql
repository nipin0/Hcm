-- 0045_state_osc_ladder_freeze.sql
-- 【止血｜临时】冻结震荡梯度手数：`state.osc_lot_ladder` 由 "0.5,1.0,1.5,2.0" 改为单档 "0.5"。
--
-- ── 为什么必须临时关掉（2026-09-16 复盘发现的 P0 缺陷）─────────────────
-- BUG-1：**计数器单位错配**。`hcm:state:osc_loss_count:{symbol}` 是**品种级**键，
--   但回写发生在**逐 ticket** 的平仓分支（`tools/position_sync.py:1114` 的 `_oid` 内
--   → `_fsm_osc_counter_writeback` → `state_machine.apply_osc_close`）。
--   而一个 FSM 信号会**扇出 master(acct 6) + follower(acct 9) = 2 笔 ticket**
--   ⇒ **每轮止损 `count += 2`**（实测：今日每轮均为 2 笔）。
--   本模块取档为 `idx = min(count, len(ladder)-1)`（`state_strategy.py:1050`）
--   ⇒ 档位只在**偶数**上取值 0 → 2 → 4… ⇒ **`idx=1`（1.0 档 = 0.02 手）结构性不可达**。
--   **实测印证**：今日 12 次入场，lot 只出现 {0.01, 0.03}，0.02 **一次都没有**；
--   ctx 快照 `frozen_loss_count=6`（偶数）。
--
-- BUG-2：同一缺陷使 **4ATR 风控预算也以 2 倍速度消耗**（`osc_atr_loss` 与
--   `osc_loss_count` 同一函数、同一回写点）⇒ 实测 ctx `frozen_atr_loss=6.705`
--   已远超 `state.osc_atr_loss_limit=4.0`，S5 于 ~15:40 提前锁止
--   （日志 `note=osc_lock_cleared`）⇒ 震荡交易被无端中断。
--
-- BUG-3：同一轮两笔 ticket 归因可能不同（实测 12:57：master=`sl` / follower=`expert`）
--   ⇒ 是否计入不确定 ⇒ **手数不可复现**（同一行情历史重放结果不同）。
--
-- ── 本迁移做什么/不做什么 ────────────────────────────────────────────
--   做：把倍率**固定为起始档 0.5**（⇒ 手数恒 0.01），消除"随机跳档"这一**风险放大**。
--   不做：这不修复 BUG-1/2/3 —— 只是**在修复前停止放大**。
--   为什么取 0.5 而不是 1.0：单档后 `idx` 恒为 0 ⇒ 手数 = `lot_base(0.02) × 0.5 = 0.01`，
--     与今日**正常档位**完全一致 ⇒ 行为变化最小，且落在更保守一侧。
--
-- ── 恢复条件（**修好后再放回**）────────────────────────────────────
--   1) `position_sync._fsm_osc_counter_writeback` 增加**轮次级幂等**（按 `signal_id`：
--      同一信号的多个 ticket 只推进一次；轮次内任一 `sl` 即按 `sl` 计入）；
--   2) 验证：`verify_state_strategy_osc.py` 新增"同一 signal_id 的 N 笔只推进 1 次"
--      与"梯度三档均可达"断言；
--   3) 回放 `replay_state_chain.py` 需补"每信号 N 笔 fan-out"能力
--      （当前是**单账户单笔**模拟 ⇒ 天生掩盖本缺陷，见其模块 docstring）。
--   4) 满足 1~3 后，用以下回滚语句放回梯度。
--
-- ── 回滚（一行）───────────────────────────────────────────────────
--   UPDATE hcm_config.metadata SET current_value='0.5,1.0,1.5,2.0'
--    WHERE config_key='state.osc_lot_ladder';
--   （同步 Redis L2：HSET hcm:config:v2 state.osc_lot_ladder "0.5,1.0,1.5,2.0"
--     + PUBLISH hcm:config:invalidate state.osc_lot_ladder）

UPDATE hcm_config.metadata
   SET current_value = '0.5',
       updated_at    = now()
 WHERE config_key = 'state.osc_lot_ladder';

SELECT config_key, default_value, current_value
  FROM hcm_config.metadata
 WHERE config_key IN ('state.osc_lot_ladder', 'state.osc_atr_loss_limit')
 ORDER BY 1;
