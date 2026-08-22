-- ═══════════════════════════════════════════════════════════════
-- HCM v2 — 经济日历初始数据
-- 版本: v1.0 | 基于 PRD v1.3 §6.3.3 事件清单
-- 包含: FOMC/NFP/CPI/PMI/ETF审批/BTC减半 等重大事件
-- 注: 此处为模板数据，实际日期需在运行时更新
-- ═══════════════════════════════════════════════════════════════

BEGIN;

-- ── FOMC 利率决议 (全部品种, CRITICAL) ────────
INSERT INTO hcm_market.event_calendar (event_name, category, event_date, importance, flat_before_min, flat_after_min, lot_scale, confidence_discount, is_active) VALUES
('FOMC 利率决议', 'all', '2026-07-30 18:00:00+00', 3, 30, 15, 0.0, 0.15, true),
('FOMC 利率决议', 'all', '2026-09-17 18:00:00+00', 3, 30, 15, 0.0, 0.15, true),
('FOMC 利率决议', 'all', '2026-11-05 19:00:00+00', 3, 30, 15, 0.0, 0.15, true),
('FOMC 利率决议', 'all', '2026-12-16 19:00:00+00', 3, 30, 15, 0.0, 0.15, true)
ON CONFLICT DO NOTHING;

-- ── NFP 非农 (全部品种, CRITICAL) ──────────────
INSERT INTO hcm_market.event_calendar (event_name, category, event_date, importance, flat_before_min, flat_after_min, lot_scale, confidence_discount, is_active) VALUES
('NFP 非农就业数据', 'all', '2026-08-07 12:30:00+00', 3, 15, 15, 0.5, 0.10, true),
('NFP 非农就业数据', 'all', '2026-09-04 12:30:00+00', 3, 15, 15, 0.5, 0.10, true),
('NFP 非农就业数据', 'all', '2026-10-02 12:30:00+00', 3, 15, 15, 0.5, 0.10, true),
('NFP 非农就业数据', 'all', '2026-11-06 13:30:00+00', 3, 15, 15, 0.5, 0.10, true),
('NFP 非农就业数据', 'all', '2026-12-04 13:30:00+00', 3, 15, 15, 0.5, 0.10, true)
ON CONFLICT DO NOTHING;

-- ── CPI 公布 (全部品种, HIGH) ──────────────────
INSERT INTO hcm_market.event_calendar (event_name, category, event_date, importance, flat_before_min, flat_after_min, lot_scale, confidence_discount, is_active) VALUES
('CPI 消费者物价指数', 'all', '2026-08-12 12:30:00+00', 2, 10, 10, 0.5, 0.08, true),
('CPI 消费者物价指数', 'all', '2026-09-11 12:30:00+00', 2, 10, 10, 0.5, 0.08, true),
('CPI 消费者物价指数', 'all', '2026-10-13 12:30:00+00', 2, 10, 10, 0.5, 0.08, true),
('CPI 消费者物价指数', 'all', '2026-11-12 13:30:00+00', 2, 10, 10, 0.5, 0.08, true),
('CPI 消费者物价指数', 'all', '2026-12-10 13:30:00+00', 2, 10, 10, 0.5, 0.08, true)
ON CONFLICT DO NOTHING;

-- ── Powell 讲话 (全部品种, MEDIUM) ─────────────
INSERT INTO hcm_market.event_calendar (event_name, category, event_date, importance, flat_before_min, flat_after_min, lot_scale, confidence_discount, is_active) VALUES
('Powell 国会证词', 'all', '2026-07-15 14:00:00+00', 1, 0, 0, 1.0, 0.05, true),
('Powell Jackson Hole 讲话', 'all', '2026-08-27 14:00:00+00', 1, 0, 0, 0.5, 0.10, true)
ON CONFLICT DO NOTHING;

-- ── PMI 公布 (部分品种) ───────────────────────
INSERT INTO hcm_market.event_calendar (event_name, category, event_date, importance, flat_before_min, flat_after_min, lot_scale, confidence_discount, is_active) VALUES
('ISM 制造业 PMI', 'metals', '2026-08-03 14:00:00+00', 1, 0, 0, 1.0, 0.03, true),
('ISM 服务业 PMI', 'metals', '2026-08-05 14:00:00+00', 1, 0, 0, 1.0, 0.03, true)
ON CONFLICT DO NOTHING;

-- ── 加密特定事件 ──────────────────────────────
INSERT INTO hcm_market.event_calendar (event_name, category, event_date, importance, flat_before_min, flat_after_min, lot_scale, confidence_discount, is_active) VALUES
('BTC ETF 审批决策', 'crypto', '2026-08-15 20:00:00+00', 3, 60, 60, 0.0, 0.20, true),
('SEC 加密监管听证会', 'crypto', '2026-09-10 14:00:00+00', 2, 0, 15, 0.5, 0.15, true),
('BTC 减半事件', 'crypto', '2028-03-25 00:00:00+00', 2, 1440, 1440, 0.5, 0.10, true)
ON CONFLICT DO NOTHING;

COMMIT;
