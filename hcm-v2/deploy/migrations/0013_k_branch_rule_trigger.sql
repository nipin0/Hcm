BEGIN;

-- 2026-08-15 K 值分支补全 + DeepSeek 规则触发：seed 新配置键。
-- 对应代码：quality_gate.py coupling_weight/_regime_of 四档分支、
--           ai_async_client.run_loop 规则触发、scheduler._read_ai_quality stale 触发。

-- 1) K 分支补全：强趋势(k>1.2) 与 衰竭(k>=2.0) 语义分离。
--    衰竭态(趋势末端/反转前兆)更信 HEXP，AI 权重压低到 w_exhaust(< w_trend)。
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, current_value, value_type, label, description)
VALUES
  ('ai.cpl.w_exhaust', 'ai_quality', 'cpl', '0.85', '0.85', 'number',
   'K分支·衰竭AI权重',
   '耦合权重 w（HEXP 权重）=0.85；衰竭态(k>=k_exhaust_min)AI 权重=1-w=0.15，更信 HEXP'),
  ('ai.cpl.k_exhaust_min', 'ai_quality', 'cpl', '2.0', '2.0', 'number',
   'K分支·衰竭阈值',
   'k >= 此值判为 EXHAUST(衰竭)；文档 2.2 分布衰竭 k=2.0~3.0'),
  ('ai.cpl.w_neutral_low', 'ai_quality', 'cpl', '0.85', '0.85', 'number',
   'K分支·中性档下沿HP权重',
   'NEUTRAL 档(0.5<k≤1.2)内 w 随 k 连续插值：k 趋近 k_range_max(0.5) 时 w=此值(最信 HEXP)，k 趋近 k_trend_min(1.2) 时 w=w_neutral=0.6')
ON CONFLICT (config_key) DO NOTHING;

-- 2) DeepSeek 规则触发：stale 阈值（距上次成功票超过此秒数即写触发标志重新校准）。
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, current_value, value_type, label, description)
VALUES
  ('ai.ds.trigger_stale_sec', 'ai_quality', 'ds', '120', '120', 'number',
   'DeepSeek重校准陈旧阈值(秒)',
   '规则触发：DeepSeek 异步票 ts 距 now 超过此值视为陈旧，_read_ai_quality 写 ai:ds:trigger:{symbol} 触发 run_loop 重新调用')
ON CONFLICT (config_key) DO NOTHING;

-- 2b) 外部因子事件态触发（2026-08-15 闭环补全）：
--     DeepSeek 触发规则除 stale 外，新增对 market_intel 外部因子的响应：
--     B) event 维度高危窗口临近 → 触发；C) composite 跳变超阈值 → 触发。
INSERT INTO hcm_config.metadata
  (config_key, category, subcategory, default_value, current_value, value_type, label, description)
VALUES
  ('ai.ds.trigger_event_min', 'ai_quality', 'ds', '0.6', '0.6', 'number',
   '外部因子·事件触发阈值',
   '规则触发B：外部因子 event 维度(0~1) >= 此值(重大财经事件窗口临近)即写 ai:ds:trigger 触发 DeepSeek 语义补盲'),
  ('ai.ds.trigger_composite_delta', 'ai_quality', 'ds', '0.15', '0.15', 'number',
   '外部因子·composite跳变触发阈值',
   '规则触发C：外部综合风险 composite(0~1) 较上次记录跳变绝对值 >= 此值即触发 DeepSeek 重新校准')
ON CONFLICT (config_key) DO NOTHING;

-- 3) 把空值键回填为 default（消除漂移）
UPDATE hcm_config.metadata
SET current_value = default_value
WHERE config_key IN ('ai.cpl.w_exhaust', 'ai.cpl.k_exhaust_min', 'ai.cpl.w_neutral_low',
                     'ai.ds.trigger_stale_sec', 'ai.ds.trigger_event_min',
                     'ai.ds.trigger_composite_delta')
  AND (current_value IS NULL OR current_value = '');

COMMIT;
