-- 0034_state_strategy.sql
-- 【2026-09-14 Phase C】策略层（状态 → 交易意图）参数 seed + 观测表扩展
--
-- 依据：docs/设计方案_信号塔状态机与交易策略重构_20260914.md §7（策略规则）§18（变更说明）
--
-- 说明：本层只产出**意图**（是否开/加仓、方向、手数倍率、箱体锚点）。
--       **SL/TP 数值不由本层计算** —— 一律由桥按「平仓配置」时段系数
--       （close.<session>.trailing_stop_distance / tp_atr_multiplier / breakeven_atr_mult ...）
--       执行，这是用户 2026-09-14 的明确决策，避免形成第二套 SL 口径。
--
-- ⚠️ 关键安全开关：state.order_enabled 默认 **false** = 只算意图、不下单。
--    这样策略层可先跑影子、观察"若要下单会怎么下"，待意图质量验证后再灰度开单
--    （符合用户 Q2「先跑 2-3 天再评估」与铁律第十一章）。

INSERT INTO hcm_config.metadata
    (config_key, category, default_value, current_value, value_type, label, description)
VALUES
    ('state.order_enabled', 'state', 'false', 'false', 'bool',
     '策略层是否真下单',
     'false=只计算并记录意图、不下单（默认）；true=按意图真实下单。'
     '开启前须先完成桥的 FSM 持仓管理分支与风控加仓豁免'),
    ('state.box.window', 'state', '20', '20', 'int',
     '入场箱体回看根数',
     '用于"价格触及箱体边界"判定的回看窗口；注意：入场箱体**不含当前 bar**'
     '（含则 close<=min(low) 恒不成立，边界永不触发）'),
    ('state.trend.slope_window', 'state', '20', '20', 'int',
     '趋势方向回归窗口',
     '顺势方向判定与移动止损回看共用（线性回归斜率 > 0 视为向上）'),
    ('state.trend.pullback_atr', 'state', '0.5', '0.5', 'number',
     '顺势回踩深度(ATR)',
     'S2 试错入场 / S3 加仓的回调深度阈值：BUY 需自近期高点回落、SELL 需自近期低点反弹达此值'),
    ('state.trend_max_adds', 'state', '2', '2', 'int',
     'S3 最大加仓次数',
     '趋势中段顺势加仓上限；每笔手数固定 base_lot（规格 10.2，不使用震荡梯度手数）'),
    ('state.osc_border_tol_atr', 'state', '0.25', '0.25', 'number',
     '震荡边界容差(ATR)',
     '"触及箱体下沿/上沿附近"的容差（ATR 倍数），对应规格 9.2 的"附近"'),
    ('state.osc_lot_ladder', 'state', '0.5,1.0,1.5,2.0', '0.5,1.0,1.5,2.0', 'string',
     '震荡梯度手数倍率',
     '按连续止损次数取值的倍率列表（规格 9.5）：0次→0.5 / 1次→1.0 / 2次→1.5 / 3次→2.0；'
     '一旦止盈立即归零（逗号分隔）')
ON CONFLICT (config_key) DO UPDATE SET
    default_value = EXCLUDED.default_value,
    current_value = EXCLUDED.current_value,
    updated_at = now();

-- ── 观测表扩展：记录"若开启会下什么单"，供影子期评估意图质量 ──
ALTER TABLE hcm_signal.market_state_log
    ADD COLUMN IF NOT EXISTS intent_action    TEXT,
    ADD COLUMN IF NOT EXISTS intent_direction TEXT,
    ADD COLUMN IF NOT EXISTS intent_lot_mult  DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS intent_reason    TEXT;

COMMENT ON COLUMN hcm_signal.market_state_log.intent_action IS
    '策略层意图动作（open/add/none）；与 intent_direction 一起用于影子期"假设成交"评估';
