-- P1: AI (DeepSeek) natural-language daily calibration diagnosis
-- One row per report_date (the daily report), summarizing all M5 regimes.
CREATE TABLE IF NOT EXISTS hcm_ai.calibration_diagnosis (
    report_date   DATE PRIMARY KEY,
    content       TEXT NOT NULL,                      -- natural-language narrative
    recommendations TEXT[],                            -- concrete tuning suggestions
    needs_review  BOOLEAN NOT NULL DEFAULT FALSE,     -- flag human intervention
    confidence    VARCHAR(16) NOT NULL DEFAULT 'low',  -- high|medium|low
    model         VARCHAR(64) NOT NULL DEFAULT 'deepseek-chat',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE hcm_ai.calibration_diagnosis IS
    'AI-generated daily calibration diagnosis (per report_date).';
COMMENT ON COLUMN hcm_ai.calibration_diagnosis.content IS
    'Natural-language diagnosis of per-regime win-rate / calibration-factor trends.';
COMMENT ON COLUMN hcm_ai.calibration_diagnosis.recommendations IS
    'Actionable tuning suggestions (e.g. raise co.gate.<regime>.trend).';
