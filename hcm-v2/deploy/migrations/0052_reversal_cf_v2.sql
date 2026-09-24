-- 0052_reversal_cf_v2.sql
-- 【2026-09-21】反转头评估能力修复 · 反事实口径 v2（幂等，可重复执行）
--
-- ── 为什么必须改（v1 口径的三处结构性缺陷，均有实测证据）──────────────
-- 背景：0011 定义的 v1 反事实口径原文是
--   「假设不调整、保持 old_sl。在【调整时刻 → 平仓时刻】区间取 M5 极值」
-- 实测（2026-09-21，709 行归因）暴露三个缺陷，使该装置**无法评价自身动作**：
--
-- 缺陷 1（截断偏差）：窗口右端用的是 `deal_time`（**实际平仓时刻**），
--   而"收紧止损"这个被评估的动作**本身就会让平仓提前** ⇒ 窗口被动作自己截断
--   ⇒ 价格还来不及走到 old_sl 就已出窗口 ⇒ `cf_exit = exit_px` ⇒ delta = 0。
--   实测：`mode='act'` 共 29 行，**delta 非零者 0 条**，sum_delta = 0.00。
--   ⇒ **"成功的收紧"被系统性判为中性。**
--
-- 缺陷 2（单边）：`cf_exit` 只有两个取值（exit_px / old_sl），且 old_sl 仅在
--   「区间触及 old_sl」时启用 ⇒ `delta = realized - cf_pnl` 几乎恒 >= 0
--   ⇒ `verdict='killed'` **结构性不可达**。实测：全期 killed = 0 条。
--   ⇒ **评估永远报不出"动错了"。**
--
-- 缺陷 3（主体错位）：唯一有非零 delta 的是 `mode='log_skip'`（**未行动**）的行
--   （实测 100 条全在 log_skip），它们度量的是"实际出场 vs 止损本该被打"的差，
--   **与反转头动作无关**；却被日报主指标 `sum_delta_r` 当成业绩（全期 564.07 R）。
--
-- ── v2 口径（本次）──────────────────────────────────────────────────
-- 反事实 = 「若 SL 保持在 old_sl」时，该仓位**先触及谁**：
--   自 adj_ts 起在**视界 H**（默认 12h，见 ai.rev.cf_horizon_hours）内做**首触赛跑**：
--     先触及 old_sl          → cf_exit = old_sl     （动对了：避免了更深的止损）
--     先触及 tp              → cf_exit = tp         （**动了错**：被提前扫出而错过止盈）
--     两者同一根 M5 内触及    → cf_exit 取 old_sl，但 verdict='ambiguous'（同 bar 无法定序）
--     视界内都未触及          → cf_exit = exit_px，delta = 0（视作动作无影响）
-- ⇒ 天然**双向**：delta>0 = saved / delta<0 = **killed**（修缺陷 2）。
--
-- 为什么这就修掉缺陷 1：窗口右端改为 `adj_ts + H`（**与动作无关的固定视界**），
--   不再受"动作让平仓提前"影响 ⇒ 反事实路径完整可见。
-- 为什么这就修掉缺陷 3：`log_skip`（未行动）行的 SL 本就等于 old_sl，
--   实际出场即反事实出场 ⇒ **shadow 行 delta 恒 0**，主指标自动只反映 act 行。
--
-- ⚠ 视界门的副作用（有意为之）：`_rev_settle_closed` 现在**只在 `adj_ts + H` 已过**
--   时才结算该行，以保证视界内数据完整 ⇒ 结算相对平仓**滞后 H**。
--   这是"可评估性"换"时效性"，H 可通过 ai.rev.cf_horizon_hours 调整。
--
-- ⚠ **历史 709 行是 v1 口径，与 v2 不可比**：`cf_basis` 为 NULL 即"v1"，
--   日报已改为只统计 `cf_basis='v2_race'`，并在 detail.legacy_v1_settled 里单列其数量。
--   这是本仓库反复出现过的同一类事故模式：**口径变了却不标注** ⇒ 新旧混算。

ALTER TABLE hcm_ai.reversal_attribution
    -- 反事实赛跑的止盈价。没有它就做不了双向赛跑（这正是缺陷 2 的根因）。
    ADD COLUMN IF NOT EXISTS tp            DOUBLE PRECISION,
    -- 反事实先触及谁：'sl' / 'tp' / 'none'（视界内都未触及）/ 'both_same_bar'（同 bar 无法定序）
    ADD COLUMN IF NOT EXISTS cf_touch      TEXT,
    -- 本次反事实使用的视界（小时）——落库以便**可复现**（口径参数变化可追溯）
    ADD COLUMN IF NOT EXISTS cf_horizon_h  DOUBLE PRECISION,
    -- 反事实口径版本：NULL/'v1_window_to_exit' = 0011 的旧口径；'v2_race' = 本迁移口径
    ADD COLUMN IF NOT EXISTS cf_basis      TEXT;

COMMENT ON COLUMN hcm_ai.reversal_attribution.tp IS
    '反事实赛跑的止盈价（0052）：与 old_sl 做首触赛跑，使 delta 可双向（saved/killed）';
COMMENT ON COLUMN hcm_ai.reversal_attribution.cf_touch IS
    '反事实先触及：sl / tp / none / both_same_bar（0052）。none 表示视界内两者都未触及 ⇒ delta 记 0（动作无影响）';
COMMENT ON COLUMN hcm_ai.reversal_attribution.cf_horizon_h IS
    '本次反事实视界（小时），口径参数落库以便复现（0052）';
COMMENT ON COLUMN hcm_ai.reversal_attribution.cf_basis IS
    '反事实口径版本（0052）：NULL=0011 旧口径(v1_window_to_exit) / v2_race=0052 双向赛跑。'
    '⚠ 两种口径的 delta 不可比，日报只统计 v2_race';

-- 结算扫描现在带"视界已过"的门（adj_ts <= now() - H），故给 (account_id, adj_ts) 一个部分索引。
CREATE INDEX IF NOT EXISTS idx_rev_attr_cf_pending
    ON hcm_ai.reversal_attribution (account_id, adj_ts)
    WHERE closed_ts IS NULL;

COMMENT ON TABLE hcm_ai.reversal_attribution IS
    '反转头 SL 调整归因记录（P3 2026-09-04；口径 v2 2026-09-21）：'
    'old_sl/new_sl/tp + 双向反事实盈亏，用于定量评估反转头净贡献。'
    'cf_basis 区分口径，跨口径不可比。';
