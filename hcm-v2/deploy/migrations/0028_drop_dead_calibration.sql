-- 0028_drop_dead_calibration.sql
-- 【2026-09-11 反冗余·彻底下线「校准时序」死链 + DROP 空列】
--
-- 依据：docs/设计方案_LightGBM解耦信号级评审与校准治理_20260911.md §5.2/§7.2；
--       2026-09-10 审计报告（calibration_daily.calib_factor 与实际胜率相关性仅 0.138）。
--
-- 背景：
--   · 前端「校准时序」页已于 2026-08-28 下线（App.tsx / Layout.tsx 注释可证），
--     其后端 calibration-history 端点遂成无消费者死端点；
--   · co.calib.* 因子键 write-only（全仓无读取端），写回链已于本日删除；
--   · daily_kpi 的 ai_opened / fused_orders / ds_only_orders 恒 0（UPGRADE 仅 passed 时发生、
--     c_ai 解耦后单源仅 lm_only），对应 SQL 计算与面板展示已删除。
--
-- 幂等：全部 IF EXISTS。

DROP TABLE IF EXISTS hcm_ai.calibration_daily;
DROP TABLE IF EXISTS hcm_ai.calibration_diagnosis;

ALTER TABLE hcm_ai.daily_kpi
  DROP COLUMN IF EXISTS ai_opened,
  DROP COLUMN IF EXISTS fused_orders,
  DROP COLUMN IF EXISTS ds_only_orders;
