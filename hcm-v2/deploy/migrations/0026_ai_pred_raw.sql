-- 0026_ai_pred_raw.sql
-- 【P0 2026-09-11 质量头校准闭环·B 项】LightGBM 三头原始分落库（滚动校准数据资产）
--
-- 依据：docs/设计方案_LightGBM解耦信号级评审与校准治理_20260911.md §校准闭环；
--       记忆 27570774（A 侧车热重载已落地，B/C 待续作）。
-- 目的：质量头（c_ai 用于耦合分闸门）的滚动重校准此前无"原始分数据源"——
--       侧车直接用已校准模型推理、从不落库 raw → 无法 join 标签 → 无法重校准。
--       本表补齐 B 项，使 C（recalibrate_quality.py）有数据可消费。
--
-- 写入方：quality_scorer sidecar 主循环（每 M5 棒每头一条，防爆表；fail-safe 异常仅告警）。
-- 读取方：recalibrate_quality.py（取窗口内 raw → join 标签 → 重拟合 calib_v*.pkl → 原子替换 → 热加载）。
--
-- 落库纪律：只写不参与交易决策；任何写入异常仅告警（fail-open），绝不影响 AI 评分主链路。
-- 与 hcm_ai.review_log 的区别：review_log=信号级评审器留痕（含 VETO）；
--   ai_pred_raw=市场快照级三头原始分（校准闭环数据源），两表并存、互不覆盖。

CREATE SCHEMA IF NOT EXISTS hcm_ai;

CREATE TABLE IF NOT EXISTS hcm_ai.ai_pred_raw (
    id            BIGSERIAL PRIMARY KEY,
    t_time        TIMESTAMPTZ NOT NULL,         -- M5 棒收盘时间（新鲜度 / 标签 join 键）
    head          TEXT NOT NULL,                -- quality / direction / entry
    model_version TEXT,
    symbol        TEXT,
    raw_proba     DOUBLE PRECISION,             -- 模型原始概率（0..1）
    cal_p         DOUBLE PRECISION,             -- 校准后概率（如有；首版留 NULL）
    pred_class    INT,                           -- 方向头：-1/0/1；其余 NULL
    extra         JSONB                          -- 其它审计字段（向后兼容扩展）
);

CREATE INDEX IF NOT EXISTS idx_ai_pred_raw_t     ON hcm_ai.ai_pred_raw (t_time DESC);
CREATE INDEX IF NOT EXISTS idx_ai_pred_raw_head ON hcm_ai.ai_pred_raw (head, symbol, t_time DESC);

COMMENT ON TABLE hcm_ai.ai_pred_raw IS
    'P0 2026-09-11: LightGBM 三头原始分落库（校准闭环 B 项）。写入方=sidecar，只写不参与决策。';
