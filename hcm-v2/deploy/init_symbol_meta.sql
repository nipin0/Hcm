-- ═══════════════════════════════════════════════════════════════
-- HCM v2 — 品种元数据初始数据
-- 版本: v1.0 | Phase 1: XAUUSD + BTCUSD
-- ═══════════════════════════════════════════════════════════════

BEGIN;

-- ── XAUUSD (贵金属) ───────────────────────────
INSERT INTO hcm_config.symbol_meta (symbol, category, display_name, base_currency, quote_currency, lot_step, min_lot, max_lot, pip_value, is_active, phase) VALUES
('XAUUSD', 'metals', '黄金/美元', 'XAU', 'USD', 0.01, 0.01, 5.0, 1.0, true, 1)
ON CONFLICT (symbol) DO NOTHING;

-- ── BTCUSD (加密货币) ─────────────────────────
INSERT INTO hcm_config.symbol_meta (symbol, category, display_name, base_currency, quote_currency, lot_step, min_lot, max_lot, pip_value, is_active, phase) VALUES
('BTCUSD', 'crypto', '比特币/美元', 'BTC', 'USD', 0.01, 0.01, 10.0, 1.0, true, 1)
ON CONFLICT (symbol) DO NOTHING;

COMMIT;
