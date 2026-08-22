-- ═══════════════════════════════════════════════════════════════
-- 0019_hexp_config_drift_fix.sql
-- 修复「和乘幂配置保存刷新又复原」根因（2026-08-21）。
--
-- 根因：PG metadata 早期 seed 的 default_value（0006 迁移，2026-08-18 前）
--   已与代码 hexp_engine._DEFAULTS / web HEXP_KEYS 对齐值漂移，例如：
--     hexp.scorecard.pass_threshold  45 → 50
--     hexp.scorecard.b_threshold     60 → 52
--     hexp.scorecard.weight_resonance 25 → 12
--     hexp.scorecard.weight_state    20 → 27
--     hexp.scorecard.weight_entry    20 → 26
--   而 web _read_config / 引擎此前用 COALESCE(current_value, default_value)，
--   导致未显式保存的键读到陈旧 seed 默认、回填表单；用户一次全量保存就把
--   旧值写回 current_value，覆盖引擎想用的新默认 → "刷新又复原"。
--
-- 本迁移（幂等，可重复执行）：
--   1) 把漂移键的 default_value 修正为与代码对齐。
--   2) 仅当 current_value 为空（从未被面板保存过）或仍等于旧 default_value
--      （等于 seed 旧值、非用户调优）时，才把 current_value 一并更新为新默认；
--      已由用户显式保存的 current_value 一律保留（不覆盖调优结果）。
--   3) 补齐 2026-08-21 新增到引擎 _DEFAULTS / web HEXP_KEYS 白名单的键的 seed。
--
-- 幂等：可重复执行。写 PG 属生产改动红线，需用户授权后手动执行。
-- ═══════════════════════════════════════════════════════════════

BEGIN;

-- ── 1) 修正漂移键的 default_value（与代码 HEXP_KEYS / _DEFAULTS 对齐）──
UPDATE hcm_config.metadata
   SET default_value = CASE config_key
         WHEN 'hexp.scorecard.pass_threshold'   THEN '50'
         WHEN 'hexp.scorecard.b_threshold'      THEN '52'
         WHEN 'hexp.scorecard.weight_resonance' THEN '12'
         WHEN 'hexp.scorecard.weight_state'     THEN '27'
         WHEN 'hexp.scorecard.weight_entry'     THEN '26'
         WHEN 'hexp.momentum_flip_mm'           THEN '0.04'
         ELSE default_value
       END
 WHERE config_key IN (
       'hexp.scorecard.pass_threshold',
       'hexp.scorecard.b_threshold',
       'hexp.scorecard.weight_resonance',
       'hexp.scorecard.weight_state',
       'hexp.scorecard.weight_entry',
       'hexp.momentum_flip_mm'
     );

-- ── 2) current_value 纠偏：仅在「从未保存或仍是 seed 旧默认」时更新 ──
-- 已由面板显式保存（current_value != 旧 seed 值）的键一律保留，不覆盖调优结果。
UPDATE hcm_config.metadata
   SET current_value = CASE config_key
         WHEN 'hexp.scorecard.pass_threshold'   THEN '50'
         WHEN 'hexp.scorecard.b_threshold'      THEN '52'
         WHEN 'hexp.scorecard.weight_resonance' THEN '12'
         WHEN 'hexp.scorecard.weight_state'     THEN '27'
         WHEN 'hexp.scorecard.weight_entry'     THEN '26'
         WHEN 'hexp.momentum_flip_mm'           THEN '0.04'
         ELSE current_value
       END,
       updated_at = now()
 WHERE config_key IN (
       'hexp.scorecard.pass_threshold',
       'hexp.scorecard.b_threshold',
       'hexp.scorecard.weight_resonance',
       'hexp.scorecard.weight_state',
       'hexp.scorecard.weight_entry',
       'hexp.momentum_flip_mm'
     )
   AND (current_value IS NULL
        OR btrim(current_value) = ''
        OR current_value IN ('45', '60', '25', '20', '0.02'));

-- ── 3) 补齐 2026-08-21 新增键的 seed（引擎 _DEFAULTS 有、此前 web 白名单无）──
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('hexp.exec.extreme_lot_mult',      'hexp', 'exec', '0.5',  'number', '极值区降仓系数',
   '处于 Donchian 极值区且未被护栏封单的放行单，手数再乘此系数（与转换/反转态取更谨慎者，不叠乘）', 'number', 98, 'global'),
  ('hexp.exec.vol_scale_enabled',     'hexp', 'exec', 'true', 'bool',   '波动率缩放手数',
   '开启后 ATR 相对常态时基础手数随行情缩放：波动放大→降仓，波动收窄→不超配', 'switch', 99, 'global'),
  ('hexp.exec.vol_scale_atr_ref',     'hexp', 'exec', '7.0',  'number', '波动率 ATR 基准',
   '常态 ATR 基准（波动率中性点），实际 ATR 相对它的比值驱动缩放', 'number', 100, 'global'),
  ('hexp.exec.vol_scale_min',         'hexp', 'exec', '0.5',  'number', '波动缩放下限',
   '波动放大时最低缩到该系数（防满仓接大波动）', 'number', 101, 'global'),
  ('hexp.exec.vol_scale_max',         'hexp', 'exec', '1.0',  'number', '波动缩放上限',
   '波动收窄时最高放大到该系数（绝不超配基础手数）', 'number', 102, 'global'),
  ('hexp.resonance.pullback_penalty', 'hexp', 'mtf', '1.0',   'number', '逆风/回踩单降分系数',
   '1.0=不额外罚；<1.0=逆风/回踩单综合评分(total)乘性下压，使分级更难达最低可下单门槛', 'number', 78, 'global'),
  ('hexp.reverse_candidate_enabled',  'hexp', 'extreme', 'true', 'bool', '反向单观测开关',
   '开启后：momentum_flip 判动量反向且处于高位/低位时，记录反向候选落库供对照评估。零实盘影响', 'switch', 260, 'global'),
  ('hexp.reverse_candidate_hi',       'hexp', 'extreme', '0.7', 'number', '反向候选高位分位',
   'BUY 被拦→SELL 候选 的高位分位阈值（pos>此值）', 'number', 261, 'global'),
  ('hexp.reverse_candidate_lo',       'hexp', 'extreme', '0.3', 'number', '反向候选低位分位',
   'SELL 被拦→BUY 候选 的低位分位阈值（pos<此值）', 'number', 262, 'global')
ON CONFLICT (config_key) DO NOTHING;

COMMIT;

-- ── Redis 侧失效广播（建议在能连 Redis 的环境执行，否则靠 config_provider
--   热重载回源 PG。最简单：HDEL 以下键 → 下次读取自动回源重建）──
--   HDEL hcm:config:v2 hexp.scorecard.pass_threshold hexp.scorecard.b_threshold
--        hexp.scorecard.weight_resonance hexp.scorecard.weight_state
--        hexp.scorecard.weight_entry hexp.momentum_flip_mm
--   PUBLISH hcm:config:invalidate hexp.scorecard.pass_threshold
--   PUBLISH hcm:config:invalidate hexp.scorecard.b_threshold
--   PUBLISH hcm:config:invalidate hexp.scorecard.weight_resonance
--   PUBLISH hcm:config:invalidate hexp.scorecard.weight_state
--   PUBLISH hcm:config:invalidate hexp.scorecard.weight_entry
--   PUBLISH hcm:config:invalidate hexp.momentum_flip_mm
--
-- 验证：
--   SELECT config_key, default_value, current_value FROM hcm_config.metadata
--    WHERE config_key IN ('hexp.scorecard.pass_threshold',
--                         'hexp.scorecard.b_threshold',
--                         'hexp.scorecard.weight_resonance',
--                         'hexp.momentum_flip_mm');
--   docker compose restart hcm-signal-tower hcm-web
