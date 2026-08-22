import React, { useEffect, useState } from 'react';
import { Box, Typography, Tooltip } from '@mui/material';
import ConfigForm, { ConfigField } from '../../components/ConfigForm';
import KpiCard from '../../components/KpiCard';
import client from '../../api/client';
import { useSymbol } from '../../contexts/SymbolContext';

// 实时值卡片用的短标签（配置键 → 紧凑展示名）
const FRIENDLY_LABEL: Record<string, string> = {
  pretrend_cooldown_seconds: 'PRE_TREND 冷却',
  trend_cooldown_seconds: 'TREND 冷却',
  fade_cooldown_seconds: 'FADE 冷却',
  range_boundary_cooldown_seconds: 'RANGE 冷却',
  neutral_cooldown_seconds: 'NEUTRAL 冷却',
  'scoring.min_adx_for_trade': 'ADX 地板(全局)',
  'scoring.M5.min_adx_for_trade': 'ADX 地板(M5)',
  regime_adx_trend: 'TREND 进入',
  regime_adx_range: 'RANGE 判定',
  'regime.trend_strong_adx_threshold': '体制强趋势',
  'scoring.trend_strong_adx_threshold': '反向阻断ADX',
  'scoring.min_score_threshold': '评分(全局)',
  'scoring.trend_min_score_threshold': 'TREND 评分（已弃用）',
  'scoring.neutral_min_score_threshold': 'NEUTRAL 评分',
  'scoring.M5.dispute_diff_threshold': 'AI 争议门槛',
  'scoring.trend_reverse_suppress_factor': '反向软抑制',
  'scoring.strong_trend_block_reverse': '强趋势降分开关',
  'scoring.strong_trend_reverse_penalty': '强趋势逆势降分',
  'scoring.h1_reverse_block_enabled': 'H1降分开关',
  'scoring.h1_reverse_penalty': '逆H1未确认降分',
  'scoring.h1_reverse_penalty_confirmed': '逆H1已确认降分',
  'scoring.neutral_rsi_enabled': 'NEUTRAL RSI均值回归',
  'scoring.live_override_enabled': '反压开关',
  'scoring.live_override_sustained_sec': '反压持续',
  'scoring.live_override_check_interval_sec': '反压间隔',
  'scoring.live_override_rate_limit_sec': '反压限流',
  'trend_reverse_cooldown_seconds': '反向冷却',
  // ── 2026-07-25 RANGE 均值回归闸门 ──
  'scoring.range_min_pct_b': 'RANGE %b 贴边',
  'scoring.range_stoch_extreme': 'RANGE Stoch 极值',
  'scoring.range_rsi_extreme_low': 'RANGE RSI 超卖',
  'scoring.range_rsi_extreme_high': 'RANGE RSI 超买',
  'scoring.range_breakout_mult': 'RANGE 真突破倍数',
  // ── 2026-07-25 校准硬闸门（多而准）──
  'scoring.calibration_enabled': '校准总开关',
  'scoring.calibration_hard_gate': '校准硬闸门',
  'scoring.calibration_gate_p': '盈亏平衡线',
  'scoring.calibration_min_n': '校准最小样本',
};

// ── 功能分组配色（每组一色，竖排卡片左侧色条） ──
// 说明：仅保留经引擎源码硬证据核实、真正被消费的“活值”。
// 死值（早鸟推理 midbar.*、体制阈值 offset/floor/ceiling、score_threshold、
// scoring.M5.trend_adx_* 等）已删除——引擎当前代码不读或只打日志、不参与计算。
const GROUP_COLORS: Record<string, string> = {
  cooldown: '#3b82f6', // 蓝 — 冷却时间
  gate: '#f59e0b',     // 琥珀 — 交易闸门 ADX
  regime: '#14b8a6',   // 青 — 体制识别 ADX
  score: '#a855f7',    // 紫 — 评分门槛
  dispute: '#ec4899',  // 粉 — AI 争议赋分
  reverse: '#ef4444',  // 红 — 反趋势保护（阻断逆势单）
  live_override: '#f43f5e', // 玫红 — 实时反压救援（反转敏捷）
  range: '#22c55e',    // 绿 — RANGE 均值回归闸门
  calibration: '#eab308', // 黄 — 校准硬闸门（多而准）
};

const buildFields = (): ConfigField[] => [
  // ── ① 冷却时间组（5 个活值）— scoring_engine.py 971-987 消费 ──
  { key: '_g_cooldown', label: '⏱ 冷却时间（按体制）— 越大越保守', type: 'section', color: GROUP_COLORS.cooldown,
    description: '各体制下新信号最小间隔（秒）。信号产生后等待这段时间再开新仓。引擎按当前体制取对应冷却值。' },
  { key: 'pretrend_cooldown_seconds', label: 'PRE_TREND 冷却(秒)', type: 'number', defaultValue: 0, min: 0, max: 7200,
    description: '市场处于 PRE_TREND（预趋势）时，新信号最小间隔。越大越保守，避免假突破。建议 120-300。' },
  { key: 'trend_cooldown_seconds', label: 'TREND 冷却(秒)', type: 'number', defaultValue: 0, min: 0, max: 7200,
    description: '市场处于 TREND（已确认趋势）时，新信号间隔。趋势确认后信号较可靠，可设较短。建议 60-180。' },
  { key: 'fade_cooldown_seconds', label: 'FADE 冷却(秒)', type: 'number', defaultValue: 300, min: 0, max: 7200,
    description: '市场处于 FADE（趋势反转）时，新信号间隔。反转需谨慎，建议 300-600 避免震荡频繁下单。' },
  { key: 'range_boundary_cooldown_seconds', label: 'RANGE 冷却(秒)', type: 'number', defaultValue: 0, min: 0, max: 7200,
    description: '市场处于 RANGE（震荡区间边界）时，新信号间隔。0=不限制。' },
  { key: 'neutral_cooldown_seconds', label: 'NEUTRAL 冷却(秒)', type: 'number', defaultValue: 0, min: 0, max: 7200,
    description: '市场处于 NEUTRAL（中性无趋势）时，新信号间隔。中性市场是差信号源，建议 300-600。' },

  // ── ② 交易闸门 ADX 组（2 个活值）— scoring_engine.py 444 / scheduler.py 454 消费 ──
  { key: '_g_gate', label: '🚦 交易闸门 ADX — 硬门槛，低于即 NO_TRADE', type: 'section', color: GROUP_COLORS.gate,
    description: 'ADX 低于此值直接 NO_TRADE，永不发方向单（RANGE 体制例外跳过此地板）。这才是真正卡交易的门槛。' },
  { key: 'scoring.min_adx_for_trade', label: 'ADX 交易地板（全局）', type: 'number', defaultValue: 18, min: 10, max: 40, step: 1,
    description: '全局 ADX 交易硬闸门（scoring_engine.py:444）。ADX 低于此值直接 NO_TRADE。RANGE 体制例外跳过。对齐 DB=18。' },
  { key: 'scoring.M5.min_adx_for_trade', label: 'ADX 交易地板（M5 覆盖）', type: 'number', defaultValue: 18, min: 10, max: 40, step: 1,
    description: 'M5 周期覆盖值（scheduler.py:2583 读取，XAUUSD M5 周期），用于 live_override 救援门控(line 662)。此值优先于全局。' },

  // ── ③ 体制识别 ADX 组（3 个活值）— regime_classifier.py 348/442/444/457 消费 ──
  { key: '_g_regime', label: '🧭 体制识别 ADX — 决定 TREND/RANGE/强趋势分类', type: 'section', color: GROUP_COLORS.regime,
    description: 'RegimeClassifier 用这三个 ADX 门槛把市场分类为 TREND / RANGE / 强趋势。改这里才真正影响体制判定。' },
  { key: 'regime_adx_trend', label: 'TREND 进入 ADX', type: 'number', defaultValue: 24, min: 15, max: 50, step: 1,
    description: 'ADX ≥ 此值进入 TREND 体制（regime_classifier.py:444）。这是真正卡体制分类的门槛，默认 24（对齐代码/DB 真值）。' },
  { key: 'regime_adx_range', label: 'RANGE 判定 ADX', type: 'number', defaultValue: 22, min: 10, max: 40, step: 1,
    description: 'RANGE 体制强度归一化基准（regime_classifier.py:457）。ADX 越低越偏 RANGE。默认 22。' },
  { key: 'regime.trend_strong_adx_threshold', label: '体制强趋势 ADX 门槛', type: 'number', defaultValue: 28, min: 15, max: 60, step: 1,
    description: 'ADX ≥ 此值判定为强趋势（regime_classifier.py:453），体制强度拉满。必须 > TREND 进入值(24)。【仅影响体制分类，与下方反向阻断无关】。默认 28。' },

  // ── ④ 评分门槛组（2 活值）— co_source.py _apply_adaptive_gate 为生产链路唯一权威门槛 ──
  { key: '_g_score', label: '📊 评分门槛 — 分数低于门槛不开仓', type: 'section', color: GROUP_COLORS.score,
    description: 'pre_score 低于门槛直接 NO_TRADE。当前真正生效的门槛：①全局基准 scoring.min_score_threshold（仅非 co_source 模式兜底 / 实时快照展示）；②NEUTRAL 体制专属 scoring.neutral_min_score_threshold（co_source 模式 range_block=False 时生效）。【scoring.trend_min_score_threshold 已弃用：引擎仅加载但闸门逻辑从不引用，改动无效】。生产链路的权威门槛是 co_source 自适应门槛(_apply_adaptive_gate)，随行情带动态变化。' },
  { key: 'scoring.min_score_threshold', label: '最低评分门槛（全局基准）', type: 'number', defaultValue: 0.15, min: 0, max: 1, step: 0.01,
    description: '全局评分基准（scoring_engine.py）。pre_score 低于此值直接 NO_TRADE。0.15=低于15%不开仓。' },
  { key: 'scoring.neutral_min_score_threshold', label: 'NEUTRAL 最低评分（专属门槛·真实键）', type: 'number', defaultValue: 0.45, min: 0, max: 1, step: 0.01,
    description: '【NEUTRAL 体制唯一真实门槛键·0-1 尺度】这是引擎实际读取并用于 NEUTRAL 体制放行的唯一键（co_source.py:_apply_adaptive_gate 第444行 base_override=self._neutral_min_score）。⚠️ 注意：不存在 co.gate.neutral.trend 这个键（旧审计误引，引擎从不读取，切勿据其调整）。历史上该键曾被误 seed 为 0.40（低于代码默认 0.45）→ 中性市门槛偏松；现已显式 seed 回 0.45 与引擎默认对齐。调高=中性市更挑剔、更少开仓；改此值经 config_provider 双写 PG+Redis 即时生效。' },

  // ── ⑤ AI 争议赋分组（1 个活值）— scheduler.py 885 消费 ──
  { key: '_g_dispute', label: '⚖️ AI 争议赋分 — 方向明确不调，分歧才调', type: 'section', color: GROUP_COLORS.dispute,
    description: '多空分数差小于门槛才判为“争议”，调 DeepSeek 破解；大于门槛则方向明确，跳过 AI 直接下单省成本。' },
  { key: 'scoring.M5.dispute_diff_threshold', label: '争议门槛 |buy-sell|', type: 'number', defaultValue: 0.05, min: 0, max: 0.5, step: 0.01,
    description: 'buy_score 与 sell_score 差值 ≤ 此值 → 争议，调 DeepSeek（scheduler.py:885）。> 此值 → 方向明确，跳过 AI。' },

  // ── ⑥ 反趋势保护组 — scoring_engine.py Plan B 门控消费（2026-08-04 解耦硬阻断→降分）──
  { key: '_g_reverse', label: '🛡 反趋势保护 — 逆势降分（下跌中少开 BUY）', type: 'section', color: GROUP_COLORS.reverse,
    description: '体制判定为趋势且方向明确时，若评分方向与趋势相反 → 降分(乘性折扣 pre_score)。强趋势(ADX≥强趋势反向阻断阈值)用更重降分系数；否则用软抑制因子。仅 M5 评分足够高才过 co_source 闸门。RANGE 体制不触发。注意：此处的"强趋势反向阻断阈值"(scoring.trend_strong_adx_threshold) 与上方体制分类的"体制强趋势 ADX 门槛"(regime.trend_strong_adx_threshold) 是两个不同键、不同用途，不要混淆。' },
  { key: 'scoring.trend_strong_adx_threshold', label: '强趋势反向阻断 ADX', type: 'number', defaultValue: 18, min: 15, max: 60, step: 1,
    description: '【强趋势降分触发线】ADX ≥ 此值且开启下方降分开关时，趋势体制下的反向单按更强系数降分（scoring_engine.py Plan B）。与 regime.trend_strong_adx_threshold(=28) 无关。默认 18。' },
  { key: 'scoring.trend_reverse_suppress_factor', label: '反向单软抑制因子', type: 'number', defaultValue: 0.40, min: 0, max: 1, step: 0.01,
    description: '非强趋势下反向单的 pre_score 乘数（scoring_engine.py Plan B）。0.40=压到 2/5。调高=放行更多弱趋势反向单。', suggested: '0.40（线上）' },
  { key: 'scoring.strong_trend_block_reverse', label: '强趋势降分开关', type: 'switch', defaultValue: true,
    description: '开启后，ADX≥强趋势反向阻断阈值(scoring.trend_strong_adx_threshold,默认18)的趋势体制下，反向单按更重系数 strong_trend_reverse_penalty 降分（不再硬阻断，避免误杀真反转）。关闭则退化为软抑制因子。默认开。', suggested: 'true（开）' },
  { key: 'scoring.strong_trend_reverse_penalty', label: '强趋势逆势降分系数', type: 'number', defaultValue: 0.30, min: 0.05, max: 1, step: 0.01,
    description: 'ADX≥强趋势反向阻断阈值时，反向单 pre_score 乘此系数（默认0.30，更重）。需 M5 评分极高(≥0.89 经 strong 带0.40)才过阈放行真反转。调高=放行更多强趋势逆势单。', suggested: '0.30' },

  // ── ⑥-b H1 主趋势方向门控（跨周期）— scoring_engine.py，2026-08-04 解耦硬阻断→逆势降分 ──
  { key: '_g_h1fw', label: '🛡 H1 主趋势方向门控（跨周期）— 逆 H1 一律降分', type: 'section', color: GROUP_COLORS.reverse,
    description: '独立补强于 M5 反向阻断(Plan B)：M5 反向阻断只比 M5 自身体制方向，无法拦住「M5 短空 vs H1 长多」的对冲式逆势单。H1 门控只看 H1 周期：当 H1 已明确方向(bias 激活)且 M5 评分反向 → 一律降分(乘性折扣)，力度随 H1 确认度分级(confirmed 更重、未确认较轻)，仅 M5 评分足够高才过 co_source 闸门；不再硬阻断，避免误杀真反转。RANGE/TRANSITION 的 H1 强度低不触发。' },
  { key: 'scoring.h1_reverse_block_enabled', label: 'H1 降分开关', type: 'switch', defaultValue: true,
    description: 'H1 主趋势方向门控总开关。开启后，逆 H1 方向单被降分。关闭则完全忽略 H1 周期（退化到纯 M5 判定）。建议保持开启。', suggested: 'true（开）' },
  { key: 'scoring.h1_reverse_penalty', label: '逆 H1 未确认降分系数', type: 'number', defaultValue: 0.55, min: 0.05, max: 1, step: 0.01,
    description: 'H1 方向未确认(direction_confirmed=False，震荡/whip)时，逆 H1 单 pre_score 乘此系数（默认0.55，较轻）。需 M5 评分较高(≥0.73 经 strong 带0.40)才过阈。', suggested: '0.55' },
  { key: 'scoring.h1_reverse_penalty_confirmed', label: '逆 H1 已确认降分系数', type: 'number', defaultValue: 0.30, min: 0.05, max: 1, step: 0.01,
    description: 'H1 方向已确认(direction_confirmed=True，近 N 根 H1 收盘坐实)时，逆 H1 单 pre_score 乘此系数（默认0.30，更重，抑制真趋势逆做）。需 M5 评分极高(≥0.89)才过阈放行。', suggested: '0.30' },

  // ── ⑥-d 重构方案 Phase1 开关（2026-08-05）— 修复强趋势不出单 / 回踩买点被杀 ──
  { key: '_g_v2', label: '🔧 重构方案 Phase1 — 强趋势/回踩买点', type: 'section', color: GROUP_COLORS.reverse,
    description: '2026-08-05 双源信号重构：强趋势 RSI 过热豁免 + 动量同向门控软化，恢复顺势单与回踩买点。' },
  { key: 'scoring.overheat_suppress_in_trend', label: '强趋势 RSI 过热豁免', type: 'switch', defaultValue: true,
    description: '开启(默认): 强趋势(ADX≥强趋势反向阻断阈值 且 体制 TREND/PRE_TREND)中 RSI 极端不再被砍，恢复顺势单。关闭则退回旧行为(任何行情砍 RSI 极端单)。', suggested: 'true（开）' },
  { key: 'scoring.lag_momentum_conflict_block', label: '动量同向门控·硬阻断', type: 'switch', defaultValue: true,
    description: '开启(默认): 滞后组主导+动量反向→硬阻断(回踩买点被杀)。关闭: 改为软折扣(×0.70)，恢复顺趋势回踩单。重构方案建议关闭以恢复最佳回踩低点。', suggested: 'true（开）' },

  // ── ⑥-c NEUTRAL RSI 均值回归（2026-07-31 新增）— scoring_engine.py 消费 ──
  { key: '_g_neutral_rsi', label: '🔵 NEUTRAL RSI 均值回归 — 低 ADX 区逢低做多 / 逢高做空', type: 'section', color: GROUP_COLORS.reverse,
    description: '仅当体制=NEUTRAL 时生效（默认关闭）。复用 RANGE 的 RSI 极值键(35/65)作为触发阈值：RSI<35 挂起 BUY、RSI>65 挂起 SELL，需第 2 根 bar 方向一致才确认放行。确认后豁免 ADX floor 与 neutral_min_score，直接在低 ADX 区做均值回归。与 H1 防火墙互斥（H1 防火墙已收窄为仅 TREND 体制生效）。' },
  { key: 'scoring.neutral_rsi_enabled', label: 'NEUTRAL RSI 均值回归总开关', type: 'switch', defaultValue: false,
    description: '灰度开关（默认关）。开启后：NEUTRAL 体制下 RSI 超卖(<35)→挂起 BUY、超买(>65)→挂起 SELL，第 2 根同向 bar 确认后放行。打开即产生产信号，建议观察胜率后再长期开启。', suggested: 'false（灰度，默认关）' },

  // ── ⑦ 实时反压救援组（5 个活值）— scheduler.py _live_override_loop 消费 ──
  { key: '_g_live', label: '🔄 实时反压救援 (live_override) — 反转敏捷', type: 'section', color: GROUP_COLORS.live_override,
    description: '持续实时 ADX 翻盘机制：强趋势期阻断逆势单后，若实时 ADX≥地板持续足够秒数则救援翻盘允许反向；含反向冷却。此前无面板，仅能经 Redis/API 配置，现已前移至此。' },
  { key: 'scoring.live_override_enabled', label: '实时反压总开关', type: 'switch', defaultValue: true,
    description: '持续反向分翻盘总开关。关闭后强趋势期被阻断的逆势单将无法被实时救援翻盘。', suggested: 'true（开）' },
  { key: 'scoring.live_override_sustained_sec', label: '实时反压持续秒', type: 'number', defaultValue: 20, min: 5, max: 120, step: 5,
    description: '实时 ADX≥地板需持续多少秒才触发翻盘救援。越小→反转首单越早（线上已调为 20）。', suggested: '20（原30）' },
  { key: 'scoring.live_override_check_interval_sec', label: '实时反压检查间隔(秒)', type: 'number', defaultValue: 5, min: 1, max: 30, step: 1,
    description: '每多少秒复查一次实时 ADX 以决定是否翻盘。', suggested: '5' },
  { key: 'scoring.live_override_rate_limit_sec', label: '实时反压限流秒', type: 'number', defaultValue: 60, min: 10, max: 300, step: 10,
    description: '两次救援之间的最小间隔，防抖避免连发。', suggested: '60' },
  { key: 'trend_reverse_cooldown_seconds', label: '反向冷却秒', type: 'number', defaultValue: 90, min: 30, max: 600, step: 30,
    description: '同向趋势单后允许反向的锁定秒数。越小→反转解锁越快（线上已调为 90）。', suggested: '90（原150）' },

  // ── ⑧ RANGE 均值回归闸门组（5 个活值）— scoring_engine.py RANGE 硬闸门消费 ──
  { key: '_g_range', label: '🟢 RANGE 均值回归闸门 — 震荡市逢高做空/逢低做多', type: 'section', color: GROUP_COLORS.range,
    description: 'RANGE 体制下，只有"价格贴布林带边 + 振荡器进入极端超买/超卖"才允许反向开仓；若收盘价突破 20 根高低点（真突破）则放弃反向。这是把震荡市高抛低吸收口为"极值贴边才做"的纪律开关。' },
  { key: 'scoring.range_min_pct_b', label: '%b 贴边阈值', type: 'number', defaultValue: 0.15, min: 0.02, max: 0.4, step: 0.01,
    description: 'BUY 要求 %b < 此值（贴下轨），SELL 要求 %b > 1-此值（贴上轨）。越小=越贴边越苛刻。对齐引擎默认 0.15。' },
  { key: 'scoring.range_stoch_extreme', label: 'Stoch %K 极值', type: 'number', defaultValue: 25, min: 5, max: 45, step: 1,
    description: '超卖 < 此值 / 超买 > 100-此值 视为极值。BUY 需 stoch_k<此值，SELL 需 stoch_k>100-此值。对齐默认 25。' },
  { key: 'scoring.range_rsi_extreme_low', label: 'RSI 超卖极值', type: 'number', defaultValue: 35, min: 20, max: 50, step: 1,
    description: 'BUY 的 RSI 极值下限：RSI < 此值也算满足"振荡器极值"（与 stoch 满足其一即可）。对齐默认 35。' },
  { key: 'scoring.range_rsi_extreme_high', label: 'RSI 超买极值', type: 'number', defaultValue: 65, min: 50, max: 80, step: 1,
    description: 'SELL 的 RSI 极值上限：RSI > 此值也算满足"振荡器极值"。对齐默认 65。' },
  { key: 'scoring.range_breakout_mult', label: '真突破放弃倍数', type: 'number', defaultValue: 1.005, min: 1.001, max: 1.02, step: 0.001,
    description: '收盘价 > 20 根高点×此值（或 < 20 根低点×此值）= 真突破，放弃反向均值回归。越小=越易判突破。对齐默认 1.005。' },

  // ── ⑨ 校准硬闸门组（4 个活值）— scoring_engine.py 校准硬闸门消费（多而准）──
  { key: '_g_calib', label: '🟡 校准硬闸门（多而准）— 按(体制,分数桶)实测胜率放行', type: 'section', color: GROUP_COLORS.calibration,
    description: '开启校准硬闸门后，仅(体制,分数桶)实测胜率 ≥ 盈亏平衡线的桶才允许开仓，低胜率桶直接 NO_TRADE。这是"多而准"的核心开关：把成交量集中在有正期望的桶里。' },
  { key: 'scoring.calibration_enabled', label: '校准总开关', type: 'switch', defaultValue: true,
    description: '总开关。关闭后校准逻辑完全不介入。对齐 DB=true。' },
  { key: 'scoring.calibration_hard_gate', label: '校准硬闸门（拦截低胜率桶）', type: 'switch', defaultValue: true,
    description: 'true=低胜率桶直接 NO_TRADE（硬闸门，多而准）；false=仅对低胜率桶 pre_score×0.85 软折扣。对齐 DB=true。' },
  { key: 'scoring.calibration_gate_p', label: '盈亏平衡线 p', type: 'number', defaultValue: 0.40, min: 0.3, max: 0.6, step: 0.01,
    description: '桶实测胜率 < 此值即拦截（R:R=1.5 时 p≥40% 才为正期望）。对齐 DB=0.40。' },
  { key: 'scoring.calibration_min_n', label: '最小样本数 n', type: 'number', defaultValue: 12, min: 5, max: 40, step: 1,
    description: '桶样本数 < 此值不信任其 p_win，放行给其他闸门（防小样本过拟合）。对齐 DB=12。' },
];

// 分组元信息（供分组概览卡片使用）
const GROUP_META: { id: keyof typeof GROUP_COLORS; name: string; desc: string; keys: string[] }[] = [
  { id: 'cooldown', name: '⏱ 冷却时间（按体制）', desc: '各体制下新信号最小间隔（秒）',
    keys: ['pretrend_cooldown_seconds', 'trend_cooldown_seconds', 'fade_cooldown_seconds', 'range_boundary_cooldown_seconds', 'neutral_cooldown_seconds'] },
  { id: 'gate', name: '🚦 交易闸门 ADX', desc: 'ADX 低于地板直接 NO_TRADE',
    keys: ['scoring.min_adx_for_trade', 'scoring.M5.min_adx_for_trade'] },
  { id: 'regime', name: '🧭 体制识别 ADX', desc: '决定 TREND/RANGE/强趋势分类',
    keys: ['regime_adx_trend', 'regime_adx_range', 'regime.trend_strong_adx_threshold'] },
  { id: 'score', name: '📊 评分门槛', desc: '分数低于门槛不开仓',
    keys: ['scoring.min_score_threshold', 'scoring.trend_min_score_threshold', 'scoring.neutral_min_score_threshold'] },
  { id: 'dispute', name: '⚖️ AI 争议赋分', desc: '分歧才调 AI 破解',
    keys: ['scoring.M5.dispute_diff_threshold'] },
  { id: 'reverse', name: '🛡 反趋势保护', desc: '趋势反向单抑制/阻断 + H1 主趋势防火墙',
    keys: ['scoring.trend_strong_adx_threshold', 'scoring.trend_reverse_suppress_factor', 'scoring.strong_trend_block_reverse', 'scoring.strong_trend_reverse_penalty', 'scoring.h1_reverse_block_enabled', 'scoring.h1_reverse_penalty', 'scoring.h1_reverse_penalty_confirmed', 'scoring.neutral_rsi_enabled'] },
  { id: 'live_override', name: '🔄 实时反压救援', desc: '实时 ADX 翻盘 + 反向冷却（反转敏捷）',
    keys: ['scoring.live_override_enabled', 'scoring.live_override_sustained_sec', 'scoring.live_override_check_interval_sec', 'scoring.live_override_rate_limit_sec', 'trend_reverse_cooldown_seconds'] },
  { id: 'range', name: '🟢 RANGE 均值回归闸门', desc: '震荡市极值贴边才反向开仓',
    keys: ['scoring.range_min_pct_b', 'scoring.range_stoch_extreme', 'scoring.range_rsi_extreme_low', 'scoring.range_rsi_extreme_high', 'scoring.range_breakout_mult'] },
  { id: 'calibration', name: '🟡 校准硬闸门（多而准）', desc: '按桶实测胜率放行/拦截',
    keys: ['scoring.calibration_enabled', 'scoring.calibration_hard_gate', 'scoring.calibration_gate_p', 'scoring.calibration_min_n'] },
];

const Threshold: React.FC = () => {
  const { selectedSymbol } = useSymbol();
  const [initialValues, setInitialValues] = React.useState<Record<string, string | number | boolean> | undefined>();
  const [formKey, setFormKey] = useState(0);

  const fetchAndSet = async (): Promise<void> => {
    if (!selectedSymbol) return;
    try {
      const { data: resp } = await client.get(`/api/signal-tower/threshold?symbol=${selectedSymbol.symbol}`);
      const d = resp.data || resp;
      const cfg = d.config || d;
      const parsed: Record<string, string | number | boolean> = {};
      if (typeof cfg === 'object' && cfg !== null) {
        const typeMap = new Map<string, string>();
        for (const f of buildFields()) {
          typeMap.set(f.key, f.type);
        }
        for (const [k, v] of Object.entries(cfg)) {
          const declaredType = typeMap.get(k);
          if (declaredType === 'text' || declaredType === 'select' || declaredType === 'textarea') {
            parsed[k] = String(v);
          } else if (declaredType === 'switch') {
            parsed[k] = String(v).toLowerCase() === 'true';
          } else {
            const num = Number(v);
            parsed[k] = (isNaN(num) || v === '' || v === null) ? String(v ?? '') : num;
          }
        }
      }
      setInitialValues(parsed);
    } catch (_e) {
      setInitialValues({});
    }
  };

  useEffect(() => {
    fetchAndSet();
  }, [selectedSymbol]);

  const handleSubmit = async (values: Record<string, string | number | boolean>): Promise<void> => {
    const updates = Object.entries(values).map(([k, v]) => ({
      config_key: k,
      value: String(v),
    }));
    await client.put('/api/signal-tower/threshold', { updates });
    await fetchAndSet();
    setFormKey(k => k + 1);
  };

  const fields = buildFields();

  return (
    <>
      <ConfigForm
        formKey={formKey}
        grouped
        memberColumns={4}
        title={`评分阈值配置（公用参数）— ${selectedSymbol?.symbol || ''}`}
        fields={fields}
        initialValues={initialValues}
        onSubmit={handleSubmit}
        apiEndpoint="PUT /api/signal-tower/threshold"
      />

      {/* 实时生效值预览（KPI 卡片分组，按 6 个功能分组排列） */}
      <Box sx={{ mt: 4 }}>
        <Typography variant="subtitle2" sx={{ color: '#94a3b8', fontWeight: 600, fontSize: '0.8rem', mb: 1.5 }}>
          📡 实时生效值（配置中心真实值 · 保存后 30s 内自动热加载）
        </Typography>
        <Box
          className="grid gap-3"
          sx={{
            gridTemplateColumns: {
              xs: '1fr',
              md: 'repeat(2, minmax(0, 1fr))',
              lg: 'repeat(4, minmax(0, 1fr))',
            },
          }}
        >
          {GROUP_META.map((g) => {
            const items = g.keys.map((k) => {
              const f = fields.find((x) => x.key === k);
              const raw = (initialValues || {})[k];
              let value: string;
              if (f?.type === 'switch') {
                value = raw === true || raw === 'true' ? '开' : '关';
              } else if (typeof raw === 'number') {
                value = f?.step && f.step < 1 ? raw.toFixed(2) : String(raw);
              } else {
                value = raw === undefined || raw === '' ? '—' : String(raw);
              }
              const help = f && 'description' in f ? (f as { description?: string }).description || '' : '';
              return {
                label: FRIENDLY_LABEL[k] || k,
                value,
                color: GROUP_COLORS[g.id],
                tooltip: help,
              };
            });
            return <KpiCard key={g.id} title={g.name} color={GROUP_COLORS[g.id]} items={items} />;
          })}
        </Box>
      </Box>

      {/* 分组概览（5 组功能分色） */}
      <Box sx={{ mt: 3, p: 2, border: '1px solid #1e293b', borderRadius: 2, backgroundColor: '#0f1117' }}>
        <Typography variant="subtitle2" sx={{ color: '#94a3b8', fontWeight: 600, fontSize: '0.7rem', mb: 1.5 }}>
          📋 功能分组（共 22 个活值 · 已剔除 18 个死值）
        </Typography>
        <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 1 }}>
          {GROUP_META.map(g => (
            <Tooltip key={g.id} title={g.desc} placement="top" arrow>
              <Box sx={{ p: 1.2, borderRadius: 1, borderLeft: `3px solid ${GROUP_COLORS[g.id]}`,
                backgroundColor: `${GROUP_COLORS[g.id]}11`, cursor: 'help' }}>
                <Typography variant="caption" sx={{ color: GROUP_COLORS[g.id], fontWeight: 600, fontSize: '0.7rem', display: 'block' }}>
                  {g.name}
                </Typography>
                <Typography variant="caption" sx={{ color: '#64748b', fontSize: '0.65rem' }}>
                  {g.keys.length} 个参数
                </Typography>
              </Box>
            </Tooltip>
          ))}
        </Box>
      </Box>
    </>
  );
};

export default Threshold;
