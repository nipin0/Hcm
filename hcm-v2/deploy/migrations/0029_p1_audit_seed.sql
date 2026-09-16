-- ═══════════════════════════════════════════════════════════════
-- 0029_p1_audit_seed.sql
-- 2026-09-11 P1a 审计修复：补 ai.lm.pullback_chase_mm_abs 的 seed。
--
-- 背景：该键被 quality_gate.py:354 读取（追单抑制微动量阈值），但此前
--   · 不在 scheduler._ai_cfg_dict 白名单（P1a 已补）
--   · PG 无 seed → 只能取 CFG_FALLBACK=0.3，面板/配置中心完全不可管。
-- 本迁移补 seed（值 = CFG_FALLBACK 现行值 0.3，行为零变化，仅使其可管）。
-- 幂等：ON CONFLICT DO NOTHING。
-- ═══════════════════════════════════════════════════════════════

BEGIN;

INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, current_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('ai.lm.pullback_chase_mm_abs', 'ai', 'lm', '0.3', '0.3', 'number',
   '追单抑制·微动量阈值 |mm|',
   'PULLBACK 态下逆动量追单抑制：SELL 且 M5 微动量 mm ≥ 此值、或 BUY 且 mm ≤ -此值 → VETO。'
   '受 ai.lm.pullback_chase_enabled 总开关控制；置 ≤0 = 关闭数值门槛。'
   '此前仅硬编码于 quality_gate 的 CFG_FALLBACK、PG 无 seed，面板不可管；本迁移补 seed。',
   'number', 91, 'global')
ON CONFLICT (config_key) DO NOTHING;

COMMIT;
