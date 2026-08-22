-- ═══════════════════════════════════════════════════════════════
-- 0004_multi_model_prompt.sql
-- 多模型提示词路由（P2 multi-model）配置种子
-- 适用：已通过 init.sql 初始化的现有库（增量迁移，向上兼容）
-- 执行时机：需用户授权后手动执行（写 PG 属生产改动红线）
-- ═══════════════════════════════════════════════════════════════
--
-- 设计决策（已与用户确认）：
--   ① 存储=按模型分配置键，零新表。配置键：
--        signal_tower.prompt.system_prompt                    (全局回退)
--        signal_tower.prompt.user_prompt_template             (全局回退)
--        signal_tower.prompt.<model>.system_prompt            (模型专属)
--        signal_tower.prompt.<model>.user_prompt_template     (模型专属)
--      <model> ∈ {ai_dynamic, co_source, manual}
--   ② 手动模式=纯镜像主账号、不调 AI（模板仅占位）。
--   ③ 回退链（scheduler）：模型专属键 → 旧全局键 → ai_invoker 硬编码兜底。
--
-- 注意：
--   * user_prompt_template 中的 {var} 由 scheduler .format() 插值；
--     JSON 示例中的字面花括号必须写成 {{ }} 才能经 .format 后保留。
--   * 本迁移仅种子 DEFAULT 值；用户可在前端「信号塔 / 提示词」按模型 Tab 覆盖。
--   * ON CONFLICT (config_key) DO NOTHING → 可安全重复执行。
-- ═══════════════════════════════════════════════════════════════

BEGIN;

-- ============================================================
-- 全局回退键（legacy fallback，scheduler 回退链终点之前一级）
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('signal_tower.prompt.system_prompt', 'signal_tower', 'prompt',
   'XAUUSD M5 quant analyst. Output compact JSON without markdown. Fields: direction,confidence,sl_atr_mult,tp_atr_mult,reason,risk. NO_TRADE if ADX<18 & narrow range.',
   'string', '系统提示词(全局)', '全局回退 system prompt；模型未单独配置时继承此值', 'textarea', 1, 'global'),
  ('signal_tower.prompt.user_prompt_template', 'signal_tower', 'prompt',
   'XAUUSD {timeframe} | {regime} r={regime_strength} | RSI={rsi} MACD={macd} ADX={adx} %b={pct_b} StochK={stoch_k} | ATR14={atr} bar_open={bar_open} close={close} bar_momentum={bar_momentum} (range/ATR) | MA={ma_alignment} pre_dir={pre_dir} pre_score={pre_score} | {trend_note} {momentum_note} | Output JSON: {{"direction":"BUY|SELL|NO_TRADE","confidence":0-1,"sl_atr_mult":1.5-2.5,"tp_atr_mult":2.0-4.0,"reason":"<50chars","risk":"<30chars"}}',
   'string', '用户提示词模板(全局)', '全局回退 user prompt 模板；支持 {var} 插值，JSON 字面花括号用 {{ }}', 'textarea', 2, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- AI 动态判断模式（ai_dynamic）：默认 DeepSeek 行为，等同全局
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('signal_tower.prompt.ai_dynamic.system_prompt', 'signal_tower', 'prompt',
   'XAUUSD M5 quant analyst. Output compact JSON without markdown. Fields: direction,confidence,sl_atr_mult,tp_atr_mult,reason,risk. NO_TRADE if ADX<18 & narrow range.',
   'string', '系统提示词(AI动态)', 'AI 动态判断模式的 system prompt', 'textarea', 10, 'global'),
  ('signal_tower.prompt.ai_dynamic.user_prompt_template', 'signal_tower', 'prompt',
   'XAUUSD {timeframe} | {regime} r={regime_strength} | RSI={rsi} MACD={macd} ADX={adx} %b={pct_b} StochK={stoch_k} | ATR14={atr} bar_open={bar_open} close={close} bar_momentum={bar_momentum} (range/ATR) | MA={ma_alignment} pre_dir={pre_dir} pre_score={pre_score} | {trend_note} {momentum_note} | Output JSON: {{"direction":"BUY|SELL|NO_TRADE","confidence":0-1,"sl_atr_mult":1.5-2.5,"tp_atr_mult":2.0-4.0,"reason":"<50chars","risk":"<30chars"}}',
   'string', '用户提示词模板(AI动态)', 'AI 动态判断模式的 user prompt 模板', 'textarea', 11, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 双源信号模式（co_source）：pre_score 已是共源校准得分，AI 仅做方向/区间复核
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('signal_tower.prompt.co_source.system_prompt', 'signal_tower', 'prompt',
   'XAUUSD M5 quant analyst with co-source calibration. pre_score is already a 0-1 co-source calibrated score (cold-start=1.0). Output compact JSON without markdown. Fields: direction,confidence,sl_atr_mult,tp_atr_mult,reason,risk. NO_TRADE if ADX<18 & narrow range, or if zone conflicts with direction.',
   'string', '系统提示词(双源)', '双源信号模式的 system prompt；强调 pre_score 已校准', 'textarea', 20, 'global'),
  ('signal_tower.prompt.co_source.user_prompt_template', 'signal_tower', 'prompt',
   'XAUUSD {timeframe} | {regime} r={regime_strength} [co_source] | RSI={rsi} MACD={macd} ADX={adx} %b={pct_b} StochK={stoch_k} | ATR14={atr} bar_open={bar_open} close={close} bar_momentum={bar_momentum} | MA={ma_alignment} pre_dir={pre_dir} pre_score={pre_score}(co-source calibrated) | ZONE lvl={zone_level} type={zone_type} str={zone_strength} | {trend_note} {momentum_note} | Output JSON: {{"direction":"BUY|SELL|NO_TRADE","confidence":0-1,"sl_atr_mult":1.5-2.5,"tp_atr_mult":2.0-4.0,"reason":"<50chars","risk":"<30chars"}}',
   'string', '用户提示词模板(双源)', '双源信号模式的 user prompt 模板；含 ZONE 结构', 'textarea', 21, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 手动模式（manual）：纯镜像主账号，不调 AI（模板仅占位）
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('signal_tower.prompt.manual.system_prompt', 'signal_tower', 'prompt',
   'Manual mirror mode. AI inference disabled — trades mirror the master account directly. Placeholder prompt (unused).',
   'string', '系统提示词(手动)', '手动模式占位 system prompt（不调用 AI）', 'textarea', 30, 'global'),
  ('signal_tower.prompt.manual.user_prompt_template', 'signal_tower', 'prompt',
   'MANUAL MIRROR MODE — AI inference disabled. Placeholder template (unused).',
   'string', '用户提示词模板(手动)', '手动模式占位 user prompt 模板（不调用 AI）', 'textarea', 31, 'global')
ON CONFLICT (config_key) DO NOTHING;

COMMIT;
