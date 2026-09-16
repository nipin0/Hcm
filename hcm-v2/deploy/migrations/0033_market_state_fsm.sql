-- 0033_market_state_fsm.sql
-- 【2026-09-14 Phase B】行情状态机（4 类 LightGBM + FSM）运行参数 seed + 观测表
--
-- 依据：docs/设计方案_信号塔状态机与交易策略重构_20260914.md §6（FSM）§9（配置键）
--
-- 本次新增两条链路：
--   1) 配置键：状态机运行参数（全部有代码消费者，见下方逐键注明）
--   2) 观测表 hcm_signal.market_state_log：每根 bar 一行，供评估
--      "状态序列是否合理/是否抖动/状态与后续行情是否吻合"
--
-- ⚠️ 刻意【不】seed `state.shadow_only`：
--   策略层（会真实下单的那一层）尚未实现，当前实现**结构上就是只观测**
--   （产物仅写 Redis 上屏 + 本表），没有任何代码路径会因该键而改变交易行为。
--   加一个无人消费的键 = 死键（铁律第十三章：新增与删除必须成对）。
--   策略层落地时再引入该键并真正 gate 下单。

-- ── 1) 运行参数 ────────────────────────────────────────────────
INSERT INTO hcm_config.metadata
    (config_key, category, default_value, current_value, value_type, label, description)
VALUES
    ('state.enabled', 'state', 'true', 'true', 'bool',
     '行情状态机总开关',
     '关闭后 scheduler 完全不加载状态模型、不推算、不落库（零开销）'),
    ('state.model_dir', 'state', '/app/review_models', '/app/review_models', 'string',
     '状态模型目录',
     '容器内路径（对应 tools/models 只读挂载）；按 lgbm_state_{tf}_v{N}_s*.txt 自动选最高版本'),
    ('state.min_conf', 'state', '0.45', '0.45', 'number',
     '单根判定最低置信',
     '四类最大概率低于此值 → 该根判定不参与防抖（既不推进也不回退）'),
    ('state.infer_fail_bars', 'state', '3', '3', 'int',
     '连续推理失败→暂停根数',
     '单根特征异常只保持原状态；连续达此根数才进 S9 暂停态'),
    ('state.debounce.k_enter', 'state', '2', '2', 'int',
     '防抖·进入新态根数',
     '连续此根数同类别才允许进入该状态'),
    ('state.debounce.k_exit', 'state', '2', '2', 'int',
     '防抖·趋势转震荡根数',
     '处于趋势态时转回震荡所需连续根数（越小退出趋势越快）'),
    ('state.debounce.k_fade', 'state', '2', '2', 'int',
     '防抖·进入衰竭根数',
     '进入 S4 趋势衰竭所需连续根数（衰竭需及时离场，通常不放大）'),
    ('state.osc_atr_loss_limit', 'state', '4.0', '4.0', 'number',
     '震荡锁止阈值(ATR)',
     '累计震荡止损达此 ATR 倍数 → S5 锁止，禁震荡开仓；由策略层/桥回写计数器'),
    ('state.fsm.flat_reset_enabled', 'state', 'false', 'false', 'bool',
     '持仓归零复位开关',
     '规格中"S4 全部平仓后回 S0"属策略层驱动复位；策略层未接线时须保持 false，'
     '否则趋势态刚进入即被持仓=0 复位，影子观测失效')
ON CONFLICT (config_key) DO UPDATE SET
    default_value = EXCLUDED.default_value,
    current_value = EXCLUDED.current_value,
    updated_at = now();

-- ── 2) 观测表 ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hcm_signal.market_state_log (
    id                BIGSERIAL PRIMARY KEY,
    symbol            TEXT        NOT NULL,
    time_frame        TEXT        NOT NULL,
    bar_open_time     TIMESTAMPTZ NOT NULL,
    state             TEXT        NOT NULL,   -- FSM 状态（S0/S1/S2/S3/S4/S5/S9）
    prev_state        TEXT,
    transitioned      BOOLEAN     DEFAULT FALSE,
    predicted_class   TEXT,                   -- 单根模型判定类别（防抖前）
    prob_oscillation  DOUBLE PRECISION,
    prob_trend_init   DOUBLE PRECISION,
    prob_trend_mid    DOUBLE PRECISION,
    prob_trend_fade   DOUBLE PRECISION,
    margin            DOUBLE PRECISION,       -- top1 - top2（判别把握）
    decided           BOOLEAN,                -- 是否达 min_conf（参与防抖）
    infer_ok          BOOLEAN,                -- 推理是否成功
    infer_reason      TEXT,                   -- ok / low_conf / bad_features / ...
    hold_only         BOOLEAN     DEFAULT FALSE,
    model_version     TEXT,
    positions_open    INTEGER     DEFAULT 0,
    note              TEXT,                   -- 迁移/保持原因（same_state/pending(...)/...）
    created_at        TIMESTAMPTZ DEFAULT now(),
    -- 幂等：同一品种+周期+bar 只留一行（重复生产/重启不产生重复样本）
    CONSTRAINT market_state_log_uniq UNIQUE (symbol, time_frame, bar_open_time)
);

COMMENT ON TABLE hcm_signal.market_state_log IS
    '行情状态机观测（Phase B 影子）：每根 bar 一行，用于评估状态序列合理性与抖动';
