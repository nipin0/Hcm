-- ═══════════════════════════════════════════════════════════════
-- 0022_rev_daily_report.sql
-- LightGBM 反转头 · 每日绩效报表聚合表（设计方案 §4）
--
-- 口径：日报用「实际 − 反事实」归因(reversal_attribution.delta)回答
--   L1 判别力（分箱校准）/ L2 动作价值（ΣΔ_R）/ L3 摩擦风险（killed）。
-- 幂等：CREATE IF NOT EXISTS + ON CONFLICT (stat_date) DO UPDATE（对齐
--   hcm_ai.daily_kpi 的 upsert-on-trade_date 模式，见 scheduler._aggregate_daily_kpi）。
-- 风险：只读聚合 + upsert 日报表，零生产写路径，不触碰 reversal_attribution 结算口径。
-- ═══════════════════════════════════════════════════════════════

BEGIN;

CREATE TABLE IF NOT EXISTS hcm_ai.rev_daily_report (
  stat_date      date PRIMARY KEY,
  -- ① 漏斗（当日新增 adj 行）
  n_trigger_pos  integer NOT NULL DEFAULT 0,   -- 触发候选仓次（浮亏>0.6ATR）
  n_requests     integer NOT NULL DEFAULT 0,   -- 评分请求
  n_scored       integer NOT NULL DEFAULT 0,   -- 有结论
  n_rev_call     integer NOT NULL DEFAULT 0,   -- 判定=反转
  n_pull_call    integer NOT NULL DEFAULT 0,   -- 判定=回踩
  -- ② 动作
  n_act          integer NOT NULL DEFAULT 0,   -- act 模式实改
  n_shadow       integer NOT NULL DEFAULT 0,   -- log 模式假设动作
  -- ③ 当日结算（closed_ts 落当日）
  n_settled      integer NOT NULL DEFAULT 0,
  n_saved        integer NOT NULL DEFAULT 0,
  n_killed       integer NOT NULL DEFAULT 0,
  n_neutral      integer NOT NULL DEFAULT 0,
  sum_delta      double precision NOT NULL DEFAULT 0,   -- 价格差口径（未乘手数，见口径注记）
  sum_delta_r    double precision NOT NULL DEFAULT 0,   -- R 归一（主指标，v0 推荐）
  avg_delta_r    double precision,                      -- 单笔平均贡献(R)
  -- ④ 明细与分箱（紧凑存储，前端解析）：表三分箱 / 表四切片 / 表五明细
  detail         jsonb NOT NULL DEFAULT '{}',
  updated_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT rev_daily_consistency CHECK (n_saved + n_killed + n_neutral <= n_settled)
);

CREATE INDEX IF NOT EXISTS idx_rev_attr_adj    ON hcm_ai.reversal_attribution(adj_ts);
CREATE INDEX IF NOT EXISTS idx_rev_attr_settle ON hcm_ai.reversal_attribution(closed_ts)
  WHERE closed_ts IS NOT NULL;

COMMENT ON TABLE hcm_ai.rev_daily_report IS
  'LightGBM 反转头每日绩效报表（只读聚合产物）。delta 为价格差口径、未乘手数、'
  '未扣点差/手续费；v0 以 R 归一(sum_delta_r)为主指标，禁止把价格差当美元金额汇报。';

COMMIT;
