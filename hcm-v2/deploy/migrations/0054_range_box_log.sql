-- 0054_range_box_log.sql
-- 【2026-09-25】Magic 55（RANGE 均值回归）箱体**逐 bar 落库**。
--
-- 背景：`hcm:live:range_box`（Redis，TTL 600s）只有"最新一根"的快照
--   ⇒ 面板只能显示**当前箱体**，无法画出"随时间移动的箱体"
--   （历史上曾因此把当前值读成历史箱体，见方案 D2 教训）。
--
-- 为什么**独立建表**而不并入 `hcm_signal.market_state_log`：
--   后者由 FSM(`scheduler._run_shadow_state`) 每 bar 写入，其
--   `ON CONFLICT (symbol,time_frame,bar_open_time) DO UPDATE` 只回填
--   `intent_* / box_*` 列。若 RANGE 侧先 UPSERT 出一行、FSM 后写入，则该行的
--   `state / prob_* / predicted_class` 将**永久为 NULL**（DO UPDATE 不覆盖它们）
--   ⇒ 会破坏既有 61 图表的全部状态数据。独立表 = 零耦合、零风险。
--
-- 口径：只落 **fast 箱**（`range.box.window`，语义 = "当前震荡幅度"，与 61 箱体同窗口
--   可比对）。slow（是否仍是震荡）/break（extremum 破界判定）暂不落库（按需再扩列）。
--   ⚠ 写入方是 `scheduler._persist_range_box`，与发布 Redis 的 fast 箱**同一次计算**
--     （不重算 ⇒ 不新增第二份箱体实现）。
--
-- 写者：hcm-signal-tower / signal_tower/scheduler.py::_persist_range_box（仅 bar 收盘主路径）
-- 读者：hcm-web / web/api/state.py 的 `/api/v1/state/kline/{symbol}`（LEFT JOIN 出 rng_fast_*）

BEGIN;

CREATE TABLE IF NOT EXISTS hcm_signal.range_box_log (
    id             BIGSERIAL PRIMARY KEY,
    symbol         TEXT             NOT NULL,
    time_frame     TEXT             NOT NULL DEFAULT 'M5',
    bar_open_time  TIMESTAMPTZ      NOT NULL,
    fast_upper     DOUBLE PRECISION,
    fast_lower     DOUBLE PRECISION,
    fast_mid       DOUBLE PRECISION,
    fast_width_atr DOUBLE PRECISION,
    fast_valid     BOOLEAN          NOT NULL DEFAULT FALSE,
    fast_reason    TEXT,
    created_at     TIMESTAMPTZ      NOT NULL DEFAULT now(),
    CONSTRAINT range_box_log_uniq UNIQUE (symbol, time_frame, bar_open_time)
);

CREATE INDEX IF NOT EXISTS idx_range_box_log_lookup
    ON hcm_signal.range_box_log (symbol, time_frame, bar_open_time DESC);

COMMENT ON TABLE hcm_signal.range_box_log IS
    'RANGE(Magic 55) 箱体逐 bar 快照（仅 fast 箱）；'
    '写者=signal-tower scheduler._persist_range_box；'
    '读者=web/api/state.py kline 端点（rng_fast_* 字段）';

COMMIT;
