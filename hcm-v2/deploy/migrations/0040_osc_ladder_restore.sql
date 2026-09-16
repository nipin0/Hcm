-- 0040_osc_ladder_restore.sql
-- 【2026-09-15】恢复震荡**梯度加仓**手数阶梯 → `0.5,1.0,1.5,2.0`
--
-- 背景：0039（灰度开闸）时我把本键**压平**为 `0.5,0.5,0.5,0.5`（取消亏损加码），
--   目的是把灰度期手数收窄到最小。但用户 2026-09-15 明确：**梯度加仓是本策略的组成部分**，
--   不应被灰度动作关掉 → 本迁移恢复原值。
--
-- ── 用户规格（原话，逐条核对实现）──────────────────────────────────
--   "只有上一笔订单止损之后，才提升下一笔入场手数：
--      连续 0 次止损 → base_lot ×0.5
--      连续 1 次止损 → base_lot ×1.0
--      连续 2 次止损 → base_lot ×1.5
--      连续 3 次止损 → base_lot ×2.0
--    一旦止盈成交，连续止损计数立刻重置为 0。
--    限制：同一品种、震荡模式下，同一方向只允许持有一单，不允许叠加同向多单。"
--
--   实现核对（全部一致，故本迁移**只改配置值、不动代码**）：
--     · 档位取值：`idx = min(consec_losses, len(ladder)-1)`；`it.lot_multiplier = ladder[idx]`
--       —— 是**倍率**（非累乘），且 4 次以上止损**封顶**在最后一档（规格未定义 4+，取保守封顶）。
--     · 计数推进：`state_machine.apply_osc_close`（**唯一实现点**，桥侧 position_sync 回写）
--         `sl` → `count += 1`；**`tp` → 两个计数器都归零**（= "止盈立刻重置为 0"）；
--         `be`/`manual`/`expert`/`stop_out` → 不计入（保本无亏损，不该抬高阶梯）。
--     · 计数真值来源：`hcm:state:osc_loss_count:{symbol}`（桥在平仓归因时写入，③号交付）。
--     · 单边 1 单：`positions_open > 0 → action=none / reason=osc_same_dir_hold`。
--       ⚠ 现状比规格**更严**：它拦的是"该品种**任意**持仓"（含反向），
--         而规格只要求拦"**同向**叠加"。故规格被满足，但**对冲（一多一空）也不允许**。
--
-- ── ⚠ 与风控单笔上限的相互作用（必须知悉）────────────────────────────
--   `base = symbol.XAUUSD.tower.lot_size = 0.02`；风控 `_apply_dynamic_lot`
--   对 state 系信号取 `lot = min(base × fsm_mult, risk.max_lot_per_trade)`：
--
--     梯度档  计算      实际下单      是否被上限削
--     ────────────────────────────────────────────
--     0.5     0.0100    0.01          否
--     1.0     0.0200    0.02          否
--     1.5     0.0300    0.03          否（恰等于上限）
--     2.0     0.0400    **0.03**      **是**（被 `risk.max_lot_per_trade=0.03` 削掉 25%）
--
--   ⇒ 第 4 档（连续 3 次止损后）**实际只到 ×1.5 的手数**，阶梯在最高档被削平。
--   ⇒ 若要 4 档全部如实生效，须把 `risk.max_lot_per_trade` 提到 ≥ 0.04
--     （⚠ 该键是**全局**的，同时约束已在跑实盘的 hexp 单，改动前须评估）。
--     本迁移**不改**该键 —— 上限削平属**保守方向**，且改全局键影响面超出本次范围。
--
-- ── 纪律 ─────────────────────────────────────────────────────────
--   `default_value` 保持 `0.5,1.0,1.5,2.0`（代码内置设计值）；`current_value` 恢复同值。
--   ⚠ 写 PG 不足够：ConfigProviderV3 是 L1(内存) → **L2(Redis hcm:config:v2)** → L3(PG)，
--     **Redis 命中即返回**。必须一并：
--       redis-cli HSET hcm:config:v2 state.osc_lot_ladder 0.5,1.0,1.5,2.0
--       redis-cli PUBLISH hcm:config:invalidate state.osc_lot_ladder

UPDATE hcm_config.metadata
   SET current_value = '0.5,1.0,1.5,2.0',
       description = '震荡**梯度加仓**手数倍率（逗号分隔，按**连续止损次数**取第 n 档）：'
                     '0次→0.5、1次→1.0、2次→1.5、3次及以上→2.0（封顶）。'
                     '**止盈成交即把连续止损计数重置为 0**（实现见 apply_osc_close 的唯一真值，'
                     '计数由桥侧 position_sync 回写 hcm:state:osc_loss_count）。'
                     '⚠ 2.0 档（base 0.02 → 0.04）会被 `risk.max_lot_per_trade=0.03` 削到 0.03。'
                     '2026-09-15 曾因灰度开闸临时压平为全 0.5，经用户确认后**恢复本值**（0040）。',
       updated_at = now()
 WHERE config_key = 'state.osc_lot_ladder';

-- ── 迁移后自检（必须看到 0.5,1.0,1.5,2.0）────────────────────────
-- SELECT config_key, default_value, current_value FROM hcm_config.metadata
--  WHERE config_key = 'state.osc_lot_ladder';
