-- 0012_ai_report.sql — AI 报表数据采集层（4 张观测表，只增不改，零下单影响）
-- 用途：为「AI 报表」模块（系统健康监控 / 信号分层统计 / 交易绩效对比 / AI快照明细）
--       提供落库数据源。全部观测性质，ai.enabled=false 时仅 sidecar 推理快照有数据。
BEGIN;

-- 1) 运行事件埋点（报表①系统健康监控：触发次数/成功率/缓存命中/熔断/降级频次）
CREATE TABLE IF NOT EXISTS hcm_ai.runtime_event (
    event_id     BIGSERIAL PRIMARY KEY,
    event_type   VARCHAR(64) NOT NULL,      -- lm_inference / ds_success / ds_fail / ds_timeout /
                                            -- ds_ratelimit / cache_hit / price_offset_fuse / degrade
    symbol       VARCHAR(32),
    status       VARCHAR(16),               -- ok / fail / degraded
    latency_ms   INTEGER,
    detail       JSONB,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_runtime_event_type_ts
    ON hcm_ai.runtime_event (event_type, created_at DESC);

-- 2) AI 闸门决策（报表②信号分层统计：HP候选 vs AI过滤后，按三模式独立统计）
CREATE TABLE IF NOT EXISTS hcm_ai.gate_decision (
    decision_id  BIGSERIAL PRIMARY KEY,
    signal_id    BIGINT,                    -- 关联 hcm_signal.signals.signal_id
    symbol       VARCHAR(32),
    direction    VARCHAR(8),
    regime       VARCHAR(16),               -- TREND / NEUTRAL / RANGE（三种市场模式）
    hp_score     NUMERIC,                   -- S_hp（HEXP 结构强度 0-100）
    c_ai         NUMERIC,                   -- 融合 AI 分 0-100
    p            NUMERIC,                   -- c_ai/100（真假概率）
    action       VARCHAR(16),               -- VETO / DOWNGRADE / HOLD / UPGRADE
    orig_grade   VARCHAR(4),
    final_grade  VARCHAR(4),
    lot_tier     VARCHAR(8),                -- low / mid / high / none
    total_score  NUMERIC,
    passed       BOOLEAN,                   -- hexp 引擎是否原本放行
    c_ai_meta    JSONB,                     -- 融合诊断 source/lm_score/ds_score/ds_age_sec
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_gate_decision_signal ON hcm_ai.gate_decision (signal_id);
CREATE INDEX IF NOT EXISTS idx_gate_decision_regime_ts
    ON hcm_ai.gate_decision (regime, created_at DESC);

-- 3) DeepSeek 异步输出（报表①调用成功率 + 报表④）
CREATE TABLE IF NOT EXISTS hcm_ai.ds_output (
    output_id        BIGSERIAL PRIMARY KEY,
    symbol           VARCHAR(32),
    fake_prob        NUMERIC,               -- DeepSeek 对 HEXP 信号真假概率 0-1
    ai_sl_coeff      NUMERIC,               -- 自适应止损系数 0.8~1.5
    continuity_score INTEGER,               -- 延续分 0-100
    status           VARCHAR(16),           -- ok / fail / timeout / ratelimit / cached
    latency_ms       INTEGER,
    cached           BOOLEAN DEFAULT FALSE, -- 是否命中缓存
    detail           JSONB,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ds_output_symbol_ts
    ON hcm_ai.ds_output (symbol, created_at DESC);

-- 4) AI 推理快照明细（报表④：检索复盘 + 模型调优）
CREATE TABLE IF NOT EXISTS hcm_ai.inference_log (
    log_id           BIGSERIAL PRIMARY KEY,
    symbol           VARCHAR(32),
    ai_score         NUMERIC,               -- LightGBM p×100
    total_score      NUMERIC,               -- 耦合综合分（解耦=HEXP scorecard_total）
    ext_factor_score NUMERIC,               -- 外部因子综合分×100
    sl_coeff         NUMERIC,
    continuity       NUMERIC,
    mode             VARCHAR(16),           -- coupled / decoupled
    model_version    VARCHAR(32),
    features         JSONB,                 -- 特征值
    snapshot         JSONB,                 -- 原始快照
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_inference_log_symbol_ts
    ON hcm_ai.inference_log (symbol, created_at DESC);

COMMIT;
