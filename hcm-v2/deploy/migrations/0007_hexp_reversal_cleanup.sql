-- ═══════════════════════════════════════════════════════════════
-- 0007_hexp_reversal_cleanup.sql
-- 和乘幂(hexp)配置键增量迁移 — 2026-08-11
--   1) 新增：反转态(_REVERSAL)减仓三键（B-1 修复）
--   2) 补种：方案 A（MTF 动量翻转 4 键）/ 方案 B（顺风对称加成）/ 精确闸门 min_grade
--            —— 这些键此前只活在代码 _DEFAULTS 里，配置中心缺失即"面板看不见、
--               flush 后回退代码默认"，此处落地为可持久、可热调的正式键
--   3) 修正：既有键的 value_type（早期手工 seed 全为 'string'，面板控件因此退化）
--   4) 删除：方案 B 已废除的 verdict 硬封弃用键（代码中已无读取点）
-- 键集合与 signal_tower/hexp_engine.py _DEFAULTS、web/api/hexp.py HEXP_KEYS 对齐
-- 幂等：可重复执行
-- ═══════════════════════════════════════════════════════════════

BEGIN;

-- ============================================================
-- 1) 反转态减仓（2026-08-11 B-1）
--    高周期 TREND_UP↔TREND_DOWN "干净反转"会绕过 TRANSITION 犹豫带直接翻向，
--    旧 transition_lot_mult 对其完全失效（越干净的反转手数反而越大）。
--    独立键 + 与 transition 取更谨慎者，避免被 transition 部署值 1.0 拉平。
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, current_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('hexp.exec.reversal_lot_mult',       'hexp', 'exec', '0.5',   '0.5',   'number', '反转态降仓系数',
   '任一周期发生 TREND_UP↔TREND_DOWN 反转时仓位再乘此值；与转换态系数取更谨慎者（不叠乘）', 'number', 99, 'global'),
  ('hexp.exec.reversal_hold_bars',      'hexp', 'exec', '3',     '3',     'int',    '反转态持有棒数',
   '反转是「态」不是瞬时事件：自触发起持有 N 根主周期棒内一律减仓；0=仅触发当次', 'number', 100, 'global'),
  ('hexp.exec.reversal_include_primary','hexp', 'exec', 'false', 'false', 'bool',   '主周期方向切换计入反转',
   '是否把主执行周期(M5)方向切换也计为反转；默认关，开启会让减仓常驻', 'switch', 101, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 2) 方案 A：MTF 迟滞机动量翻转（消除高周期滞后，让裁决"跟手"）
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, current_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('hexp.mtf.flip_enabled',    'hexp', 'state', 'true', 'true', 'bool',   'MTF 动量翻转开关',
   '趋势态下若近 N 根收盘斜率决定性反向持续 K 次，立即翻向，不等 trend_score 跌破退出线', 'switch', 60, 'global'),
  ('hexp.mtf.flip_window',     'hexp', 'state', '8',    '8',    'int',    '翻转斜率窗口 N',
   '计算该周期近 N 根收盘斜率', 'number', 61, 'global'),
  ('hexp.mtf.flip_bars',       'hexp', 'state', '2',    '2',    'int',    '翻转确认次数 K',
   '决定性反向持续 K 次 update 即翻向（防单根噪音假翻）', 'number', 62, 'global'),
  ('hexp.mtf.flip_slope_mult', 'hexp', 'state', '1.0',  '1.0',  'number', '翻转斜率强度倍数',
   '净位移/波动包络 ≥ 此值即判"决定性"；调高=更迟钝，调低=更敏感', 'number', 63, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 3) 方案 B：顺/逆风对称降分（顺风加成，替代已废除的无脑 bonus）
--    + 精确信号闸门 min_grade
-- ============================================================
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, current_value, value_type, label, description, ui_control, ui_order, scope) VALUES
  ('hexp.resonance.tailwind_bonus', 'hexp', 'mtf',  '0.0', '0.0', 'number', '顺风加成系数',
   '方案 B 对称降分：顺风温和加成；默认 0 = 完全对称，不再顺风虚涨推闸', 'number', 76, 'global'),
  ('hexp.min_grade',                'hexp', 'core', 'C',   'C',   'string', '最低可下单分级',
   'S/A/B/C：低于此级的信号仍完整落库供观测，但不产生交易方向（NO_TRADE）', 'text', 15, 'global')
ON CONFLICT (config_key) DO NOTHING;

-- ============================================================
-- 4) 修正既有键的 value_type（早期手工 seed 一律写成 'string'，
--    导致面板控件与类型校验退化）。仅修类型，不动取值。
-- ============================================================
UPDATE hcm_config.metadata SET value_type = 'bool'
  WHERE config_key = 'hexp.mtf.flip_enabled' AND value_type <> 'bool';
UPDATE hcm_config.metadata SET value_type = 'int'
  WHERE config_key IN ('hexp.mtf.flip_window', 'hexp.mtf.flip_bars') AND value_type <> 'int';
UPDATE hcm_config.metadata SET value_type = 'number'
  WHERE config_key IN ('hexp.mtf.flip_slope_mult', 'hexp.resonance.tailwind_bonus') AND value_type <> 'number';

-- 固化 current_value：current_value 为空时配置中心回落 default_value，
-- 一旦 default 被后续迁移改动就会静默漂移。此处把生效值写实。
UPDATE hcm_config.metadata
   SET current_value = default_value
 WHERE config_key IN ('hexp.resonance.penalty', 'hexp.resonance.tailwind_bonus')
   AND (current_value IS NULL OR btrim(current_value) = '');

-- ============================================================
-- 4b) 修复被写坏的体制感知权重方案（hexp.factor_weights_json）
--
--   现象：引擎每轮刷屏 WARNING "hexp weight schemes parse failed, fallback
--   neutral"，趋势/震荡自适应权重静默失效、长期只跑中性权重。
--   根因：web/api/hexp.py 读取时对该键做 json.loads 后以 dict 回给面板，面板
--   原样回传，旧写入逻辑用 str(value) 落库 → 存成 Python repr（单引号）→
--   引擎 json.loads 必然抛错。即"在面板点过一次保存就永久打坏"。
--   代码侧已改为 json.dumps（_serialize_value），此处修数据。
--   判据：合法 JSON 必以 {" 开头；否则回落 default_value（内容等价的合法 JSON）。
-- ============================================================
UPDATE hcm_config.metadata
   SET current_value = default_value
 WHERE config_key = 'hexp.factor_weights_json'
   AND (current_value IS NULL OR current_value NOT LIKE '{"%');

-- ============================================================
-- 5) 删除弃用键（方案 B 已废除 verdict 硬封；代码中均无读取点）
--    hexp.mtf.long_threshold  —— "verdict≥0.5 禁止开空"铁律
--    hexp.mtf.short_threshold —— "verdict≤-0.5 禁止开多"铁律
--    hexp.resonance.bonus     —— 顺风无脑加成，已由 tailwind_bonus 取代
--    留着只会让面板显示"可调且生效"，误导调参。
-- ============================================================
DELETE FROM hcm_config.metadata
 WHERE config_key IN (
   'hexp.mtf.long_threshold',
   'hexp.mtf.short_threshold',
   'hexp.resonance.bonus'
 );

COMMIT;

-- 执行后需同步 Redis 缓存（配置中心 L2）：
--   HSET   hcm:config:v2 hexp.exec.reversal_lot_mult 0.5 \
--                        hexp.exec.reversal_hold_bars 3 \
--                        hexp.exec.reversal_include_primary false
--   HDEL   hcm:config:v2 hexp.mtf.long_threshold hexp.mtf.short_threshold hexp.resonance.bonus
--   PUBLISH hcm:config:v2:updated hexp
--
-- 验证：
--   SELECT config_key, current_value, value_type FROM hcm_config.metadata
--    WHERE config_key LIKE 'hexp.exec.reversal%' OR config_key LIKE 'hexp.mtf.flip%';
--   SELECT count(*) FROM hcm_config.metadata WHERE config_key LIKE 'hexp.%';
