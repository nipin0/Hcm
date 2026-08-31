-- 005_liquidity_snapshots.sql
-- 流动性因子持久化表（与 macro_snapshots / sentiment_snapshots 同构）
-- 供训练侧 quality_features.load_env 与推理侧 quality_scorer._env_features 同表同源读取。
-- 代码侧 _persist_liquidity 也会 CREATE TABLE IF NOT EXISTS 自愈，本文件用于显式迁移/审计。

CREATE TABLE IF NOT EXISTS hcm_market.liquidity_snapshots (
    id               SERIAL PRIMARY KEY,
    category         TEXT         NOT NULL,   -- 资产类别，XAUUSD 对应 'metals'
    liquidity_score REAL,                    -- 0~1 流动性分(越低=越枯竭/滑点风险越高)
    snapshot_time    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_liq_snap_cat_time
    ON hcm_market.liquidity_snapshots (category, snapshot_time);
