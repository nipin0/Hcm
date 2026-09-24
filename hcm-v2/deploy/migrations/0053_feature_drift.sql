-- 0053_feature_drift.sql
-- 【2026-09-22】特征漂移监控（PSI）落库表 + 配置键 —— 把阶段 A 唯一被证实有效的新增能力接入生产
--
-- 依据：docs/方案_LightGBM_FSM独立信号模块重构_评估_20260922.md（§A 阶段 A 实施结论）
--   阶段 A 判决为「不进入阶段 B」：原方案的「方向融合进标签」路线被数据证伪
--   （可实现收益口径下 recall_long=0.038 / box=0.003，模型只捕获 oracle 上限的 0.8%）。
--   但其中的 **PSI 漂移监控** 是**独立于该路线**、且现系统确实缺失的能力，故单独移植。
--
-- 为什么**新建表**而不是给 `hcm_signal.market_state_log` 加列：
--   PSI 是「低频 × 多特征」观测（每 `state.drift.every_bars` 根一次、每个特征一个值，
--   当前契约 27 个）。若塞进逐 bar 表需加 27 列，且 99% 行恒为 NULL；更关键的是
--   PSI 只在「按 (symbol, tf, 窗口) 聚合」后才有意义，与逐 bar 语义不同源。
--
-- 为什么默认**全关**：
--   本迁移只**建能力**，不改任何行为。`state.drift.enabled=false` ⇒
--   scheduler 完全不采样（零查询、零写入）。回滚 = 置回 false（秒级）或 DROP TABLE。
--
-- ⚠️ `state.drift.auto_disable`（超标自动关闭状态机信号）默认 **false**：
--   它是**唯一会改变交易行为**的键。启用前必须同时满足（对齐 state_infer.py:59-63 的验收门范例）：
--     ① 连续 ≥`block_streak`(3) 次采样 verdict=block；
--     ② 同期 `market_state_log` 复算显示信号质量确实下降；
--     ③ 存在人工恢复路径（不得自动开回来）。
--
-- 幂等：CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS / ON CONFLICT DO NOTHING。

CREATE TABLE IF NOT EXISTS hcm_signal.feature_drift_log (
    id            bigserial PRIMARY KEY,
    symbol        text             NOT NULL,
    time_frame    text             NOT NULL,
    bar_open_time timestamptz      NOT NULL,
    -- 基准来源："adaptive"（前一窗口自比较）或 "file"（离线基准 npz）
    ref_kind      text             NOT NULL,
    window_bars   integer          NOT NULL,
    n_ref         integer,
    n_cur         integer,
    max_psi       double precision,
    -- PSI 最大的特征名（排查"哪个特征漂了"的第一线索）
    max_col       text,
    verdict       text,
    blocked       boolean          NOT NULL DEFAULT false,
    -- 逐特征 PSI 全量（{feature: psi}），供面板下钻
    psi_json      jsonb,
    -- 本次是否真的执行了"自动关闭信号"（默认恒 false）
    auto_disabled boolean          NOT NULL DEFAULT false,
    created_at    timestamptz      NOT NULL DEFAULT now()
);

COMMENT ON TABLE hcm_signal.feature_drift_log IS
    '特征分布漂移（PSI）采样：每 state.drift.every_bars 根 bar 一条；verdict∈{ok,warn,block,unknown}';

COMMENT ON COLUMN hcm_signal.feature_drift_log.ref_kind IS
    '"adaptive"=用前一窗口当基准（零依赖，测近期突变）；"file"=读离线基准 npz（测相对训练集的漂移）';

COMMENT ON COLUMN hcm_signal.feature_drift_log.psi_json IS
    '逐特征 PSI 全量 {特征名: 值}；值为 null 表示该特征不可算（基准退化/样本不足）';

COMMENT ON COLUMN hcm_signal.feature_drift_log.auto_disabled IS
    '本次是否触发了"超标自动关闭信号"（仅 state.drift.auto_disable=true 且连续 block 达标时为 true）';

-- 幂等键：同一 (symbol, tf, bar, 基准来源) 只允许一行 —— 重跑/重连不会产生重复采样
CREATE UNIQUE INDEX IF NOT EXISTS uq_feature_drift_log
    ON hcm_signal.feature_drift_log (symbol, time_frame, bar_open_time, ref_kind);

-- 面板按时间倒序取"最近 N 次采样"
CREATE INDEX IF NOT EXISTS ix_feature_drift_log_time
    ON hcm_signal.feature_drift_log (bar_open_time DESC);

-- ══════════════════ 配置键（只 seed 有代码消费者的键，不制造死键）══════════════════
-- 消费者：scheduler._run_shadow_state 的 `_maybe_sample_drift`（每 30s 热重载经 _load_state_cfg）
INSERT INTO hcm_config.metadata
    (config_key, category, default_value, current_value, value_type, label, description)
VALUES
    ('state.drift.enabled', 'state', 'false', 'false', 'bool',
     '特征漂移监控总开关',
     '开启后 scheduler 每 state.drift.every_bars 根 bar 采样一次 PSI 并落 hcm_signal.feature_drift_log。'
     '默认 false = 完全不采样（零查询、零写入，零行为变更）；回滚 = 置回 false'),

    ('state.drift.every_bars', 'state', '12', '12', 'int',
     '漂移采样频率（根 bar）',
     '每 N 根 bar 计算一次。默认 12 = M5 每小时一次。为什么降频：PSI 需对多个特征做'
     '「分位分箱 × 数百样本」计算，逐 bar 全量会挤占信号塔实时预算（FSM 求值实测中位 1.6s）'),

    ('state.drift.window_bars', 'state', '480', '480', 'int',
     '漂移采样窗口（根 bar）',
     '基准与当前窗口各取这么多根。默认 480 = M5 的 40 小时。下界 60（不足则无法分位分箱）'),

    ('state.drift.ref_kind', 'state', 'adaptive', 'adaptive', 'string',
     'PSI 基准来源',
     'adaptive=用前一个窗口当基准（零依赖、立即可用、测近期分布突变）；'
     'file=读 model_dir 下 lgbm_state_{TF}_drift_base.npz（测相对训练集的漂移，更准但需先离线产出基准）。'
     'file 缺失时自动回退 adaptive，绝不报错'),

    ('state.drift.psi_warn', 'state', '0.1', '0.1', 'float',
     'PSI 告警阈值',
     'PSI 经验分界：<0.1 稳定；0.1~0.2 轻微漂移（告警）；>0.2 严重漂移（block）'),

    ('state.drift.psi_block', 'state', '0.2', '0.2', 'float',
     'PSI 严重漂移阈值',
     '超过则 verdict=block。是否据此自动关闭信号由 state.drift.auto_disable 决定（默认不关）'),

    ('state.drift.bins', 'state', '10', '10', 'int',
     'PSI 分箱数',
     '两侧必须**共用同一套箱边界**（只从基准分布导出）。若各按自身分位切箱，'
     '箱标签不一致会使 PSI 爆到无意义值（实测 16.1 vs 正确 2.07），'
     '或退化为两个均匀直方图使 PSI≈0（漂移被掩盖）'),

    ('state.drift.auto_disable', 'state', 'false', 'false', 'bool',
     'PSI 超标时自动关闭状态机信号（⚠ 会改变交易行为）',
     '唯一会改变交易行为的漂移键。默认 false。启用前必须同时满足：'
     '①连续 ≥state.drift.block_streak 次 verdict=block；②同期 market_state_log 复算显示信号质量下降；'
     '③存在人工恢复路径（不得自动开回）。属核心机制变更，须走变更说明与灰度'),

    ('state.drift.block_streak', 'state', '3', '3', 'int',
     '自动关闭所需的连续 block 次数',
     '仅当 state.drift.auto_disable=true 时生效。默认 3 —— 避免单次采样噪声误关')
ON CONFLICT (config_key) DO NOTHING;
