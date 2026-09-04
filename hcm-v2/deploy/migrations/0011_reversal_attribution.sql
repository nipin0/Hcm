-- 0011_reversal_attribution.sql
-- 【P3 2026-09-04】反转头 SL 调整的归因记录表（幂等，可重复执行）
--
-- 目的：定量回答"反转头究竟省了多少钱 / 误杀了多少"，而不是靠行情事后解释。
-- 机制：每次反转头实际调整 SL 时写入一行（记录 old_sl / new_sl / 调整时快照）；
--       持仓平仓后回填实际盈亏与"若不调整"的反事实盈亏，二者之差即反转头的净贡献。
--
-- 反事实口径（counterfactual）：
--   假设不调整、保持 old_sl。在「调整时刻 → 平仓时刻」区间取 M5 极值：
--     BUY ：若区间最低价 <= old_sl → 反事实在 old_sl 止损；否则持仓未触发，
--            以区间末价（实际平仓价）离场。
--     SELL：对称，用区间最高价 >= old_sl 判断。
--   只在"反转收紧"方向上有意义（回踩放宽时 old_sl 更紧，反事实为更早起损）。

CREATE SCHEMA IF NOT EXISTS hcm_ai;

CREATE TABLE IF NOT EXISTS hcm_ai.reversal_attribution (
    id              BIGSERIAL PRIMARY KEY,
    account_id      INTEGER NOT NULL,
    ticket          BIGINT  NOT NULL,

    -- ── 持仓静态信息 ──
    symbol          TEXT,
    direction       TEXT,
    entry_price     DOUBLE PRECISION,

    -- ── 调整时快照 ──
    adj_ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    price_at_adj    DOUBLE PRECISION,   -- 调整时现价
    old_sl          DOUBLE PRECISION,   -- 调整前 SL（反事实基准）
    new_sl          DOUBLE PRECISION,   -- 调整后 SL（实际采用）
    atr             DOUBLE PRECISION,
    score           DOUBLE PRECISION,   -- 反转头评分
    cutoff          DOUBLE PRECISION,   -- 判定阈值
    is_reversal     BOOLEAN,            -- true=判反转(收紧) / false=判回踩(放宽)
    mode            TEXT,               -- act / log
    dd_atr          DOUBLE PRECISION,   -- 调整时浮亏（ATR 倍数）

    -- ── 平仓结算（回填）──
    closed_ts       TIMESTAMPTZ,
    exit_price      DOUBLE PRECISION,   -- 实际平仓价
    realized_pnl    DOUBLE PRECISION,   -- 实际盈亏（价格差口径）
    cf_exit_price   DOUBLE PRECISION,   -- 反事实平仓价
    cf_pnl          DOUBLE PRECISION,   -- 反事实盈亏
    delta           DOUBLE PRECISION,   -- realized - cf（>0 表示反转头贡献为正）
    verdict         TEXT                -- saved(减亏) / killed(误杀) / neutral
);

CREATE INDEX IF NOT EXISTS idx_rev_attr_lookup
    ON hcm_ai.reversal_attribution (account_id, ticket);

-- 未结算记录的部分索引，供桥每轮快速捞取待结算行
CREATE INDEX IF NOT EXISTS idx_rev_attr_open
    ON hcm_ai.reversal_attribution (account_id)
    WHERE closed_ts IS NULL;

COMMENT ON TABLE hcm_ai.reversal_attribution IS
    '反转头 SL 调整归因记录（P3 2026-09-04）：old_sl/new_sl + 反事实盈亏，用于定量评估反转头净贡献';
