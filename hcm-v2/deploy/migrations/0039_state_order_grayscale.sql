-- 0039_state_order_grayscale.sql
-- 【2026-09-15】**FSM 灰度开闸**：state.order_enabled=false → true（用户授权）
--
-- 目的（用户指令）：让 ③（`position_sync._fsm_osc_counter_writeback` 震荡计数回写）
--   与 ④（`mt5_bridge` 的 FSM 移动止损分支）**有真实信号可验**，并把整条链路打通。
--
-- ⚠ 这是本方案**第一次真实下单**。故本迁移的每一条都经过前置核查（见下），
--   并刻意**只动 FSM 专属键**（动全局键会同时改变已在跑实盘的 hexp 行为）。
--
-- ── 前置核查（全部实测，非推断）────────────────────────────────────
--   1. **风控会放行**：`risk_min_confidence=0.10`，而 FSM 的 `confidence=dec.margin`
--      （模型决策边际，实测 0.234）⇒ 通过。margin<0.10 的 bar 会被风控拒（自然过滤）。
--   2. **手数管道**：`stream_consumer._apply_dynamic_lot` 对 `signal_mode` 以 state 开头的
--      信号走**专用分支**：`lot = min(base × fsm_lot_multiplier, risk.max_lot_per_trade)`，
--      且 `return`（**不参与** confidence 分档，否则会恒落 low 档）。
--      `base` = `symbol.XAUUSD.tower.lot_size` = **0.02**（≤ max_lot 0.03 故被采用）。
--   3. **magic 映射**：`signal_publisher.SIGNAL_MODE_MAGIC` 有 `state_osc→61` /
--      `state_trend→62`，与桥的 `_FSM_MAGICS=(61,62)` 一致 ⇒ 桥的 FSM 分支（④）会接管。
--   4. **`action=add` 能执行**：全桥**仅一处** `get("action")`（是 manual_mirror 专用）
--      ⇒ 普通信号路径**不读 action**，`open` 与 `add` **都按"开一笔新仓"执行** ——
--      正合 FSM 设计（`fsm_adds_used = state* 持仓数 − 1`，加仓 = 再开一笔）。
--   5. **桥已加载 ③④ 代码**：桥于 12:54 重启，`verify_position_sync_hook.py` 12 项全过。
--   6. **`shadow_only` 不门控**：塔内仅上屏硬编码、桥内 0 引用（已一并修正为反映真实状态）。
--
-- ── 本次实际手数（可预知，故写死在此备查）──────────────────────────
--   路径        计算                    灰度后手数
--   ───────────────────────────────────────────────────
--   S1 震荡     base 0.02 × ladder 0.5   **0.01**（=- 最小手数）
--   S2/S3 趋势  base 0.02 × 1.0          **0.02**
--   （`state.osc_lot_ladder` 本次一并**压平**，见下）
--
-- ── 一并做的事：压平 S1 梯度手数（**灰度期临时收窄**）──────────────
--   原值 `0.5,1.0,1.5,2.0` 是**按连续止损次数递增**的（0.01→0.02→0.03→0.03 封顶）。
--   它在 FSM 专属键里是**唯一**能收窄手数的杠杆（`base` 与 hexp 共用，**不得动**：
--   改 `risk.lot_base` / `symbol.XAUUSD.tower.lot_size` / `risk.max_lot_per_trade`
--   都会同时改变已在跑实盘的 hexp 单）。
--   ⇒ 压平为 `0.5,0.5,0.5,0.5`：S1 恒 0.01，**取消亏损加码**。
--   ⚠ 副作用：**灰度不覆盖"梯度手数"这一功能**（S5 锁止仍会被覆盖，它只依赖计数器）。
--   灰度结束应恢复原值（原值见本文件注释与 0034 历史）。
--
-- ── 仍然生效的既有护栏（本次**未改**）────────────────────────────
--   `risk.max_lot_per_trade` = 0.03   单笔硬顶
--   `risk.max_concurrent_signals` = 5 最大并发笔数
--   `risk.cooldown_minutes` = 5       同品种冷却（⚠ S3 加仓若在 5 分钟内会被冷却拦）
--   `risk.max_daily_loss` = 200       日内亏损熔断（**真闸**）
--   FSM 自身：S5 锁止（4ATR 震荡累计）/ max_adds=2 / S4 禁新开 + 收紧止损
--   ⚠ `risk.max_total_exposure` = **499** —— 形同不限，**不构成约束**，故真实约束来自上面几条。
--
-- ── 回滚（无需重启：本键是热重载，30s 内生效）──────────────────────
--   UPDATE hcm_config.metadata SET current_value='false'
--     WHERE config_key='state.order_enabled';
--   redis-cli HSET hcm:config:v2 state.order_enabled false
--   redis-cli PUBLISH hcm:config:invalidate state.order_enabled

-- ── 开闸 ─────────────────────────────────────────────────────────
UPDATE hcm_config.metadata
   SET current_value = 'true',
       description = '是否真下单（**默认 false = 只算意图、纯观测**）。'
                     '2026-09-15 用户授权**灰度开闸**：目的是让 ③position_sync 的震荡计数'
                     '回写与 ④桥侧 FSM 移动止损分支有真实信号可验。'
                     '灰度期手数：S1=0.01（ladder 已压平）、S2/S3=0.02；'
                     '护栏：单笔≤0.03、并发≤5、冷却 5min、日内亏损 200。'
                     '回滚：current_value=false（30s 热重载生效，无需重启）',
       updated_at = now()
 WHERE config_key = 'state.order_enabled';

-- ── 压平 S1 梯度手数（灰度期临时收窄；原值 0.5,1.0,1.5,2.0）────────
UPDATE hcm_config.metadata
   SET current_value = '0.5,0.5,0.5,0.5',
       description = '震荡梯度手数倍率（逗号分隔，按**连续止损次数**取第 n 档）。'
                     '⚠ **2026-09-15 灰度期临时压平为全 0.5**：取消"亏损后加码"，'
                     '使 S1 每单一律 base×0.5=0.01（最小手数）。'
                     '理由：base 与 hexp 共用不可动，本键是 FSM 专属的**唯一**收窄杠杆。'
                     '**灰度结束后应恢复 0.5,1.0,1.5,2.0**（原设计值，见 0034/§44）。',
       updated_at = now()
 WHERE config_key = 'state.osc_lot_ladder';

-- ── 迁移后自检（必须看到 order_enabled=true）──────────────────────
-- SELECT config_key, current_value FROM hcm_config.metadata
--  WHERE config_key IN ('state.order_enabled','state.osc_lot_ladder');
--
-- ⚠ 写 PG 不足够：ConfigProviderV3 是 L1(内存) → **L2(Redis hcm:config:v2)** → L3(PG)，
--   **Redis 命中即返回**。必须一并：
--     redis-cli HSET hcm:config:v2 state.order_enabled true
--     redis-cli HSET hcm:config:v2 state.osc_lot_ladder 0.5,0.5,0.5,0.5
--     redis-cli PUBLISH hcm:config:invalidate state.order_enabled
--     redis-cli PUBLISH hcm:config:invalidate state.osc_lot_ladder
