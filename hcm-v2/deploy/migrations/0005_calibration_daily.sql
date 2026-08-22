-- 0005: 每日校准快照表（AI 自我发展闭环的「日历」）
-- 每个交易日(date(bar_time))，按 M5 体制累计胜率→校准因子，存为时序快照，
-- 供前端「校准时序」页回放各体制校准因子/胜率的演化轨迹。
-- 公式与 local_calibrator 一致：calib = clamp(1.0 + (win_rate - 0.5), 0.6, 1.4)
-- 冷启动：累计样本 < 20 或 样本日 < 7 → calib=1.0。

CREATE TABLE IF NOT EXISTS hcm_ai.calibration_daily (
    report_date  DATE         NOT NULL,            -- 样本归属交易日
    m5_regime    TEXT         NOT NULL,            -- NEUTRAL/TREND/TREND_FADE/RANGE/...
    trades       INTEGER      NOT NULL DEFAULT 0,  -- 当日该体制已平仓笔数
    wins         INTEGER      NOT NULL DEFAULT 0,  -- 当日该体制盈利笔数
    losses       INTEGER      NOT NULL DEFAULT 0,  -- 当日该体制亏损笔数
    win_rate     NUMERIC(5,4) NOT NULL DEFAULT 0,  -- 当日胜率(小数)
    calib_factor NUMERIC(6,4) NOT NULL DEFAULT 1.0,-- 截至该日累计校准因子(小数)
    sample_days  INTEGER      NOT NULL DEFAULT 0,  -- 该体制累计样本日数(冷启动判据)
    cold_start   BOOLEAN      NOT NULL DEFAULT TRUE,-- 是否仍处冷启动(因子=1.0)
    applied      BOOLEAN      NOT NULL DEFAULT FALSE,-- 是否为当前生效校准
    computed_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (report_date, m5_regime)
);

CREATE INDEX IF NOT EXISTS ix_calibration_daily_date
    ON hcm_ai.calibration_daily (report_date);
CREATE INDEX IF NOT EXISTS ix_calibration_daily_regime
    ON hcm_ai.calibration_daily (m5_regime);
