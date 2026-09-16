-- 0025_review_log.sql
-- 【P0 2026-09-11 信号级评审改造】LightGBM 信号级评审留痕表（幂等，可重复执行）
--
-- 依据：docs/设计方案_LightGBM解耦信号级评审与校准治理_20260911.md §3.5
-- 目的：修复"评审黑洞"——现状 VETO 信号不落库（scheduler.py 的 VETO 分支直接 return），
--       导致"被 AI 拦掉的信号后来该不该拦"无法回算，评审机制无法迭代。
--
-- 写入方：scheduler 内【信号级评审器 Reviewer】（P2 阶段上线，shadow/canary/active 三态）
-- 读取方：每日评审报表 + 分层命中回算（评审效果的唯一数据资产）
--
-- 与 hcm_ai.gate_decision 的区别：
--   gate_decision  = 现行 quality_gate（市场快照级、耦合分）的决策留痕（保留，勿动）
--   review_log     = 新【信号级】评审器的留痕（含 VETO 全量），两表并存、互不覆盖
--
-- 落库纪律：只写不参与交易决策；任何写入异常仅告警（fail-open），绝不影响信号流。

CREATE SCHEMA IF NOT EXISTS hcm_ai;

CREATE TABLE IF NOT EXISTS hcm_ai.review_log (
    id                BIGSERIAL PRIMARY KEY,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),

    -- ── 信号标识（VETO 早于 signal_id 生成时为 NULL，故允许为空）──
    signal_id         BIGINT,                      -- hcm_signal.signals.signal_id（可空）
    symbol            TEXT,
    direction         TEXT,                        -- 信号方向 BUY / SELL（hexp 决定，AI 不改）
    session           TEXT,                        -- asia / europe / us
    regime            TEXT,                        -- 市况体制
    signal_mode       TEXT,                        -- hexp / live_override / ...

    -- ── 信号属性（评审输入，修 train/serve skew 的关键）──
    entry_price       DOUBLE PRECISION,
    sl_price          DOUBLE PRECISION,
    tp_price          DOUBLE PRECISION,
    hp_score          DOUBLE PRECISION,            -- HEXP 原始评分
    grade             TEXT,                        -- HEXP 原始等级

    -- ── 特征快照引用 ──
    feat_bar_time     TIMESTAMPTZ,                 -- 所用特征快照的 M5 bar_time（新鲜度追溯）
    feat_missing_ratio DOUBLE PRECISION,           -- 特征缺失率（>阈值走降级链）

    -- ── 评审输出（ReviewVerdict）──
    action            TEXT,                        -- PASS / DOWNGRADE / VETO / pass_through
    review_score      DOUBLE PRECISION,            -- 整合评审分 ∈[0,1]（已校准）
    dir_prob          DOUBLE PRECISION,            -- 方向头原始概率
    dir_pred          TEXT,                        -- 方向头预测 BUY/SELL/HOLD
    entry_prob        DOUBLE PRECISION,            -- 买点头概率
    quality_prob      DOUBLE PRECISION,            -- 质量头概率
    reason_codes      TEXT,                        -- 逗号分隔：DIR_CONFLICT/LOW_ENTRY/...
    model_version     TEXT,                        -- 评审模型版本（追溯）
    calib_version     TEXT,                        -- 校准器版本
    latency_ms        DOUBLE PRECISION,            -- 评审耗时（自证毫秒级）
    mode              TEXT,                        -- 评审器运行态 shadow / canary / active
    extra             JSONB                        -- 其它审计字段（向后兼容扩展）

);

CREATE INDEX IF NOT EXISTS idx_review_log_created ON hcm_ai.review_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_review_log_signal  ON hcm_ai.review_log (signal_id);
CREATE INDEX IF NOT EXISTS idx_review_log_sym     ON hcm_ai.review_log (symbol, created_at DESC);

COMMENT ON TABLE hcm_ai.review_log IS
    'P0 2026-09-11: LightGBM 信号级评审留痕（含 VETO）。方案 §3.5；写入方=Reviewer(P2)，只写不参与交易决策。';
