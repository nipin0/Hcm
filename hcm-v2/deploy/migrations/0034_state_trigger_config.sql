-- 0034_state_trigger_config.sql
-- 【2026-09-15】趋势起点触发器（rise|donchian）与方向模块（trend_direction）运行参数 seed
--
-- 依据：docs/设计方案_信号塔状态机与交易策略重构_20260914.md §29–§31
--
-- 为什么引入这些键：状态机原先的"趋势态入口"由 4 类模型 argmax + 防抖**间接推导**，
-- 走前式多折实测为 漏检 20% / 误报 67.8% / 中位**滞后 +3.5 根**；
-- 改为**显式起点触发器**后为 漏检 0.4% / 误报 16.6% / 中位**提前 −1.0 根**（§30.3）。
-- 这些键即该链路的**真实消费者**（scheduler._load_trigger_config → trend_trigger/trend_direction）。
--
-- 纪律（对齐 0033 的先例）：**只 seed 有代码消费者的键**，不制造死键。
--   · `state.trigger.required` 消费者：state_machine.decide（趋势态入口的门）
--   · 其余键消费者：scheduler._load_trigger_config（每 30s 热重载进 _trigger_cfg）
--
-- ⚠️ `state.trigger.required` 默认 **false**：
--   它是一道"趋势态入口必须由触发器确认"的门。当前策略层 `state.order_enabled=false`
--   （只算意图不下单），故保持 false = 行为与实施前完全一致；
--   开启属**核心机制变更**，须先出变更说明并走影子→灰度（方案 §21.2），不得直接置 true。

INSERT INTO hcm_config.metadata
    (config_key, category, default_value, current_value, value_type, label, description)
VALUES
    ('state.trigger.required', 'state', 'false', 'false', 'bool',
     '趋势态入口须触发器确认',
     '开启后 S2/S3 的进入必须同时满足：起点触发器响 且 方向≠NONE。'
     '实测触发器漏检 0.4%/误报 16.6%/提前 1 根，远优于按类别推导（20%/67.8%/滞后 3.5）。'
     '属核心机制变更，开启前须走变更说明与灰度'),

    ('state.trigger.donchian_w', 'state', '20', '20', 'int',
     '起点触发器·突破回看根数',
     'close 突破前 w 根极值即触发。w=20 由走前式多折数据驱动标定得出'
     '（w≤20 全部零漏检，w=20 误报最低 9.9% 且中位最早 −4.0，见 §27.3）'),

    ('state.trigger.rise_m', 'state', '3', '3', 'int',
     '起点触发器·上升速率跨度(根)',
     'ΔP = P(起点)[t] − P(起点)[t−m]。P 在起点前若干根逐步抬高，'
     '故必须用**动态**ΔP 而非水平过阈（后者 ON 段横跨多事件，实测漏检 142/中位滞后，见 §29.4）'),

    ('state.trigger.rise_thr', 'state', '0.2948', '0.2948', 'number',
     '起点触发器·ΔP 阈值',
     '按**目标触发率分位**标定（M5/L=5 实测 0.2948）；'
     '⚠ 该值随模型与周期变化：若配置中心未显式设置，代码回落到起点模型 meta 的阈值。'
     '不得改用"最大 F1"规则标定 —— 实测其随 base rate 漂移（触发率 40.9%→87.0%）'),

    ('state.trigger.use_rise', 'state', 'true', 'true', 'bool',
     '起点触发器·启用 rise 分支',
     '关闭后触发器退化为纯突破检测（误报更低但提前量不稳）'),

    ('state.trigger.use_donchian', 'state', 'true', 'true', 'bool',
     '起点触发器·启用突破分支',
     '关闭后触发器退化为纯 ΔP（提前更早但漏检上升）'),

    ('state.dir.slope_thr_atr', 'state', '1.0', '1.0', 'number',
     '方向·ATR 归一斜率阈值',
     '方向 = ATR 归一回归斜率 + ±DI 同向确认（trend_direction.py）。'
     '阈值以 ATR 为单位才可跨品种（原始斜率单位"价格/根"，XAUUSD 与 EURUSD 差千倍）'),

    ('state.dir.debounce_bars', 'state', '3', '3', 'int',
     '方向·K线防抖根数',
     '确认方向需最近 k 根原始方向全等，否则输出 none（方向模糊）。'
     '采用**纯窗口函数**（非带 carry 的迟滞）以保证离线与线上逐点一致')
ON CONFLICT (config_key) DO UPDATE SET
    default_value = EXCLUDED.default_value,
    current_value = EXCLUDED.current_value,
    updated_at = now();

-- ── 3) 买点参数（L4，2026-09-15）──────────────────────────────────
-- 依据：tools/eval_entry_timing.py 在**同一信号集**上对比 8 种买点规则（H=12 根）：
--   A 触发即入            R = −0.336
--   B 等回调 0.5ATR/等5根  R = −0.289   （等回调优于即入）
--   C 即入 + 滤振幅>1.5ATR R = **−0.249**（最优，较 A 提升 +0.09R）
-- ⚠ 全部为负期望 → **买点不是瓶颈**（根因是 M5 方向符号反向，方案 §20/§34）。
--   该键只把实测更优的那一项落地；趋势腿在 M5 上是否启用需另行裁决。
INSERT INTO hcm_config.metadata
    (config_key, category, default_value, current_value, value_type, label, description)
VALUES
    ('state.trend.spike_atr_max', 'state', '1.5', '1.5', 'number',
     '买点·追涨过滤阈值(ATR)',
     '首次入场（S2 试错 / S3 无仓首建）时，若当前 bar 振幅 > 此 ×ATR 则放弃入场。'
     '实测该过滤在同一信号集上把平均 R 从 −0.336 提升到 −0.249；不加在加仓路径上（未实测）')
ON CONFLICT (config_key) DO UPDATE SET
    default_value = EXCLUDED.default_value,
    current_value = EXCLUDED.current_value,
    updated_at = now();

-- ── 观测表补列：让影子数据能验证三件套各自的贡献 ──────────────────
-- 为什么要补：状态机的评估必须能区分"这次进入趋势态是触发器带来的、还是类别带的"，
-- 以及"方向是否为 none"。缺这些列则无法回算触发器与方向的真实贡献。
ALTER TABLE hcm_signal.market_state_log
    ADD COLUMN IF NOT EXISTS age_bars        INTEGER,
    ADD COLUMN IF NOT EXISTS direction       TEXT,
    ADD COLUMN IF NOT EXISTS trigger_on      BOOLEAN,
    ADD COLUMN IF NOT EXISTS trigger_reason  TEXT;

COMMENT ON COLUMN hcm_signal.market_state_log.age_bars IS
    '当前状态已持续 bar 数：承载"初生/中段"的区分（§25，不靠分类预测）';
COMMENT ON COLUMN hcm_signal.market_state_log.direction IS
    '方向模块结果 up/down/none（§20）；none 时禁止趋势开仓（规避方向模糊的假趋势）';
COMMENT ON COLUMN hcm_signal.market_state_log.trigger_on IS
    '起点触发器是否触发（§30，rise|donchian）';
COMMENT ON COLUMN hcm_signal.market_state_log.trigger_reason IS
    '触发来源：rise / donchian / rise+donchian / none';
