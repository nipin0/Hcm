-- 0023_tail_close_attribution.sql
-- 【2026-09-04 路径 C】同向尾单止损（tail_stop guard）触发时的组级归因记录表（幂等，可重复执行）
--
-- 目的：为「rev 提前触发 + AI 门控（路径 A/B）」铺数据——记录每次 tail guard 全平同向组时
--       的触发上下文与组快照，供离线反事实评估"0.5ATR 硬切 vs 让反转头决策"孰优。
-- 反事实口径（后续离线脚本）：
--   假设不触发全平、保持各组 old_sl。在「触发时刻 → 各仓实际平仓时刻」区间取 M5 极值：
--     BUY ：区间最低价 <= old_sl → 反事实在 old_sl 止损；否则继续持有到实际平仓价。
--     SELL：对称。
--   delta = 实际组净落袋(≈触发时浮盈) - 反事实组净落袋 → 量化 tail guard 净贡献。
--
-- 说明：独立建表，不复用 reversal_attribution，避免污染 rev_daily 报表语义
--       （该报表按 adj_ts/closed_ts 统计反转头 SL 调整的 saved/killed，口径不同）。

CREATE SCHEMA IF NOT EXISTS hcm_ai;

CREATE TABLE IF NOT EXISTS hcm_ai.tail_close_attribution (
    id             BIGSERIAL PRIMARY KEY,
    account_id     INTEGER NOT NULL,

    -- ── 触发上下文 ──
    symbol         TEXT,
    direction      TEXT,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    grp_n          INTEGER,               -- 触发时同向组仓数（≥2）
    grp_be_n       INTEGER,               -- 组内已保本仓数（≥1）

    -- ── 尾单信息 ──
    tail_ticket    BIGINT,
    tail_entry     DOUBLE PRECISION,
    tail_dd_atr    DOUBLE PRECISION,      -- 尾单触发时浮亏（ATR 倍数，>threshold_mult）
    atr            DOUBLE PRECISION,
    threshold_mult DOUBLE PRECISION,      -- 生效阈值（close.tail_stop_atr_mult）

    -- ── 组快照（离线反事实用）──
    positions      JSONB,                 -- [{ticket, entry, sl, volume, profit}]
    grp_float_pnl  DOUBLE PRECISION,      -- 触发时组净浮盈（货币；≈全平落袋近似）

    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tail_close_ts
    ON hcm_ai.tail_close_attribution (symbol, ts DESC);

COMMENT ON TABLE hcm_ai.tail_close_attribution IS
    '同向尾单止损触发归因（2026-09-04 路径C）：每次 tail guard 全平同向组的上下文+组快照，供反事实评估与路径A/B建模';
