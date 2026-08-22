-- 0013_ai_daily_kpi.sql — AI 每日 KPI 聚合表（方案 B：日表 + 后台每小时聚合）
-- 用途：为「AI 报表」模块提供每日汇总数据源（系统健康 / 信号分层 / 交易绩效 /
--       AI 真实盈亏贡献）。全部观测性质，零下单影响。
-- 聚合触发：信号塔 scheduler._ai_daily_kpi_loop 每小时跑一次，自动补算昨日整日
--           KPI（00:05 后触发），历史可回溯（跑一次回填过去 N 天）。
-- AI 盈亏归因：用 gate_decision.signal_id ↔ orders.signal_id 做 JOIN（不碰 orders 表结构）。
BEGIN;

CREATE TABLE IF NOT EXISTS hcm_ai.daily_kpi (
    kpi_id              BIGSERIAL PRIMARY KEY,
    trade_date          DATE NOT NULL UNIQUE,        -- 自然日（按 open_time 归日）
    -- ① 系统健康监控
    lm_inferences       INTEGER DEFAULT 0,           -- LightGBM sidecar 推理次数
    ds_calls            INTEGER DEFAULT 0,           -- DeepSeek 调用次数
    ds_success          INTEGER DEFAULT 0,
    ds_fail             INTEGER DEFAULT 0,
    ds_timeout          INTEGER DEFAULT 0,
    cache_hits          INTEGER DEFAULT 0,           -- AI 分缓存命中
    fuse_events         INTEGER DEFAULT 0,           -- 价格偏移熔断次数
    degrade_events      INTEGER DEFAULT 0,           -- 降级次数
    -- ② 信号分层统计
    hp_candidates       INTEGER DEFAULT 0,           -- HEXP 候选信号数
    ai_passed           INTEGER DEFAULT 0,           -- AI 闸门放行数
    ai_vetoed           INTEGER DEFAULT 0,           -- AI 否决数
    ai_upgraded         INTEGER DEFAULT 0,          -- AI 升级（含 ai_opened 赋能打开）
    ai_downdgraded      INTEGER DEFAULT 0,          -- AI 降级
    ai_opened           INTEGER DEFAULT 0,           -- AI 赋能打开（hexp 未放行 → 打开）
    -- ③ 交易绩效对比
    total_orders        INTEGER DEFAULT 0,           -- 当日成交订单数
    total_pnl           NUMERIC DEFAULT 0,           -- 当日总盈亏（USD）
    win_orders          INTEGER DEFAULT 0,           -- 盈利订单数
    loss_orders         INTEGER DEFAULT 0,           -- 亏损订单数
    -- ④ AI 真实盈亏贡献（JOIN gate_decision ↔ orders）
    ai_enhanced_orders  INTEGER DEFAULT 0,           -- AI 赋能成交单数（signal_id 命中 gate_decision）
    ai_enhanced_pnl     NUMERIC DEFAULT 0,           -- AI 赋能单盈亏合计
    non_ai_pnl          NUMERIC DEFAULT 0,           -- 非 AI 单盈亏合计
    ai_contrib_ratio    NUMERIC DEFAULT 0,           -- ai_enhanced_pnl / total_pnl（占比）
    -- 融合来源分布
    fused_orders        INTEGER DEFAULT 0,           -- source=fused 单
    lm_only_orders      INTEGER DEFAULT 0,           -- source=lm_only 单
    ds_only_orders      INTEGER DEFAULT 0,           -- source=ds_only 单
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_daily_kpi_date
    ON hcm_ai.daily_kpi (trade_date DESC);

COMMIT;
