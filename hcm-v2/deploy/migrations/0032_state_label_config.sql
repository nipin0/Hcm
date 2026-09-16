-- 0032_state_label_config.sql
-- 【2026-09-14】行情状态机（4 类）标签口径与窗口参数 seed（幂等，可重复执行）
--
-- 依据：docs/设计方案_信号塔状态机与交易策略重构_20260914.md §2（标签）§14.2（标定）
--
-- 背景：tools/build_state_labels.py 的标签口径阈值此前只存在于代码 CFG_FALLBACK，
-- 生产不可审计、面板不可调、M15/H1 标定也无统一入口。现纳入 hcm_config.metadata，
-- 值为 **XAUUSD M5 网格搜索标定结果**（2026-09-14，20061 根 K 线），
-- 与代码 fallback 完全一致 → 未执行本迁移的环境行为不变。
--
-- 窗口语义（经 N 敏感性数据修正）：
--   state.horizon_bars       主窗口 N：oscillation / trend_init / trend_mid 使用
--   state.horizon_bars_fade  衰竭窗口：trend_fade 使用（默认=主窗口；设 20 可提 fade 召回）
--   注：曾拟"trend_init 用短窗口"，被实测否决（init 召回 N=8 为 0.072 < N=12 的 0.125），
--       故 init 与主窗口一致，勿再改回短窗口。

INSERT INTO hcm_config.metadata
    (config_key, category, default_value, current_value, value_type, label, description)
VALUES
    ('state.horizon_bars', 'state', '12', '12', 'int',
     '状态主窗口(根)',
     'oscillation/trend_init/trend_mid 的未来观察窗口 N（用户区间 10–20）'),
    ('state.horizon_bars_fade', 'state', '12', '12', 'int',
     '状态衰竭窗口(根)',
     'trend_fade 专用窗口；设 20 可提升衰竭召回（实测 0.239→0.447），代价是类别占比变化'),

    ('state.label.er_osc', 'state', '0.25', '0.25', 'number',
     '震荡·效率比上限',
     'Kaufman 效率比 ER ≤ 此值 且 净位移 ≤ disp_osc → oscillation'),
    ('state.label.disp_osc', 'state', '0.25', '0.25', 'number',
     '震荡·净位移上限(ATR)',
     'ATR 归一净位移上限，与 er_osc 同时满足才判震荡'),
    ('state.label.er_trend', 'state', '0.30', '0.30', 'number',
     '趋势·效率比下限',
     'ER ≥ 此值 且 净位移 ≥ disp_min → trend_mid'),
    ('state.label.disp_min', 'state', '0.30', '0.30', 'number',
     '趋势·净位移下限(ATR)',
     'ATR 归一净位移下限（趋势类与初生后段判定共用）'),
    ('state.label.er_fade', 'state', '0.35', '0.35', 'number',
     '衰竭·后段效率比上限',
     '衰竭窗口后半程 ER ≤ 此值（或最大逆行 ≥ fade_ret_atr）→ 视为推进衰竭'),
    ('state.label.disp_init', 'state', '0.45', '0.45', 'number',
     '初生·前段净位移上限(ATR)',
     '前半程净位移 ≤ 此值 = 前段仍在压缩，是 trend_init 的前提'),
    ('state.label.adx_trend', 'state', '22.0', '22.0', 'number',
     '衰竭·ADX 下限',
     '判 trend_fade 要求当前 ADX ≥ 此值（即当下必须处于趋势中）'),
    ('state.label.adx_slope_min', 'state', '3.0', '3.0', 'number',
     '衰竭·ADX 下降幅下限',
     '衰竭窗口内 ADX 下降量 ≥ 此值才判衰竭'),
    ('state.label.fade_ret_atr', 'state', '0.60', '0.60', 'number',
     '衰竭·最大逆行(ATR)',
     '衰竭窗口内最大逆行 ≥ 此值 → 视为趋势已被打断'),

    ('state.label.er_band', 'state', '0.015', '0.015', 'number',
     '置信过滤·ER 边界带',
     'ER 落在阈值 ± 此带内 → 样本过模糊，剔除不参与训练'),
    ('state.label.disp_band', 'state', '0.04', '0.04', 'number',
     '置信过滤·位移边界带',
     '净位移落在阈值 ± 此带内 → 剔除'),
    ('state.label.adx_band', 'state', '1.0', '1.0', 'number',
     '置信过滤·ADX 边界带',
     'ADX 落在阈值 ± 此带内 → 剔除')
ON CONFLICT (config_key) DO UPDATE SET
    default_value = EXCLUDED.default_value,
    current_value = EXCLUDED.current_value,
    updated_at = now();
