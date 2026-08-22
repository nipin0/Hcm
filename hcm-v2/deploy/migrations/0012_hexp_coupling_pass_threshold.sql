-- ═══════════════════════════════════════════════════════════════
-- 0012_hexp_coupling_pass_threshold.sql
-- 2026-08-17 方案甲：HEXP 已过闸信号的「耦合二次放行门槛」。
-- 仅在 coupled 模式 + c_ai 有效时生效；AI 断联/解耦/未启用 → HEXP 兜底放行。
-- 阈值与面板 ai.cpl.tier_low 现行值对齐（=50）。
-- 幂等：ON CONFLICT DO NOTHING；已存在则不改。
-- ═══════════════════════════════════════════════════════════════

BEGIN;

INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, current_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('hexp.coupling_pass_threshold', 'hexp', 'coupling', '50.0', '50.0', 'number',
   'HEXP 已过闸·耦合二次放行门槛',
   'coupled 模式 + c_ai 有效时，耦合总分(total=w(k)·hp+(1-w)·c_ai) 必须 ≥ 此值才放行；'
   '低于此值 → 拦掉（HEXP 已过闸也不下单）。AI 断联/解耦/未启用 → 忽略此门槛（HEXP 兜底放行）。'
   '置 0 = 不设门槛（完全回退现状）。与 ai.cpl.tier_low 对齐。',
   'number', 90, 'global')
ON CONFLICT (config_key) DO NOTHING;

COMMIT;
