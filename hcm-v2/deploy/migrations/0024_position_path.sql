-- 0024_position_path.sql
-- 【P0 2026-09-05 贴合行情专项】持仓路径（MFE/MAE 曲线）持久化表（幂等，可重复执行）
--
-- 目的：为「精准入场点 + 移动止盈收益最大化」建数据底座。此前持仓过程中的最大有利偏移
--       (MFE) / 最大不利偏移 (MAE) 只在反转头里逐 bar 现算（reversal_features），从未持久化
--       → 无法回答三个核心问题：
--         1) 入场点好不好：该点位入场后到底能吃到多少空间（MFE_R）？
--         2) 止损合不合理：被扫损的单，MFE 曾到过多少（差一点就赢？）
--         3) 出场漏了多少：实现 R ÷ MFE_R = MFE 捕获率，即移动止盈吃掉了多少该吃的行情
--       没有这张表，入场点模型与出场参数寻优(P1/P2) 都只能靠拍脑袋。
--
-- 采样口径（桥侧 _position_path_sample，仅主号，fail-open）：
--   · 每根 M5 bar 每仓落一条（bar 内多次调用只更新内存，不重复写库）
--   · mfe/mae 用「开仓以来 M5 K 线 high/low」+ 当前 tick 补齐未收盘 bar（bar 级精度）
--   · init_sl_price = 首次见到的 SL（R 基准）；SL=0 时离线用 2×ATR 回退
--   · 只落原始事实（价格/极值/SL/TP/浮盈），R 归一、ATR、world、regime、session 全部
--     由离线脚本用 hcm_market.klines 补齐（口径统一、可反复重算，桥侧不做行情计算）
--
-- 下游：
--   P0 基线报告  tools/position_path_report.py
--   P1 入场点模型：标签 E[MFE_R]、P(先达1R)、最优挂单偏移 δ*
--   P2 出场寻优  ：回放不同 breakeven/trail 参数，比较总 R 与 MFE 捕获率

CREATE SCHEMA IF NOT EXISTS hcm_ai;

CREATE TABLE IF NOT EXISTS hcm_ai.position_path (
    id             BIGSERIAL PRIMARY KEY,
    account_id     INTEGER NOT NULL,

    -- ── 持仓标识 ──
    ticket         BIGINT       NOT NULL,
    symbol         TEXT        NOT NULL,
    direction      TEXT        NOT NULL,          -- BUY / SELL
    open_time      TIMESTAMPTZ NOT NULL,          -- 持仓开仓时间（MT5 pos.time，秒→UTC）
    volume         DOUBLE PRECISION,

    -- ── 采样时点 ──
    bar_time       TIMESTAMPTZ NOT NULL,          -- 当前 M5 bar 开盘时间（采样粒度）
    bars_in_trade  INTEGER     NOT NULL DEFAULT 0,-- 开仓以来经过的 M5 根数
    is_closed      INTEGER     NOT NULL DEFAULT 0,-- 1=平仓时补写的最后一条

    -- ── 原始事实（价格口径，不做归一）──
    entry_price    DOUBLE PRECISION NOT NULL,
    last_price     DOUBLE PRECISION,              -- 采样时 bid/ask（按方向取不利侧）
    mfe_px         DOUBLE PRECISION,              -- 最大有利偏移（价格差，正）
    mae_px         DOUBLE PRECISION,              -- 最大不利偏移（价格差，正）
    sl_price       DOUBLE PRECISION,              -- 当前 SL（0=无）
    tp_price       DOUBLE PRECISION,              -- 当前 TP（0=无）
    init_sl_price  DOUBLE PRECISION,              -- 开仓时 SL（R 基准；0=开仓无 SL）
    profit         DOUBLE PRECISION,              -- 当前浮动盈亏（账户货币）

    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 同仓同 bar 只留一条（桥侧按 bar 变化写库，幂等防重复）
CREATE UNIQUE INDEX IF NOT EXISTS uq_position_path_ticket_bar
    ON hcm_ai.position_path (ticket, bar_time);
CREATE INDEX IF NOT EXISTS idx_position_path_bar
    ON hcm_ai.position_path (bar_time);
CREATE INDEX IF NOT EXISTS idx_position_path_sym_open
    ON hcm_ai.position_path (symbol, open_time);
CREATE INDEX IF NOT EXISTS idx_position_path_ticket
    ON hcm_ai.position_path (ticket);

COMMENT ON TABLE hcm_ai.position_path IS
    'P0 持仓路径样本：每根 M5 每仓一条 MFE/MAE 快照，供入场点模型与移动止盈参数寻优（离线回放）';
