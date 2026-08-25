/** 和乘幂信号配置 (Hexp / Harmonic Power-Mean) — 独立信号源参数面板.
 *
 * 依据《和乘幂信号策略开发文档》：
 *   HP-Score = (Σ w_i·|f_i|^k)^(1/k) × sign(Σ w_i·f_i)，幂指数 k 随市况自适应
 *   （趋势市凸增强 k>1，震荡市凹收敛 k<1）；M5 主执行周期 + H1/H4/D1 共振裁决；
 *   新信号源：M1 微结构动量幂律；6 维评分卡 S/A/B/C/红灯分级。
 *
 * 禁硬编码：全部参数经配置中心(PG↔Redis)读写，保存即热生效，无需重启。
 * 激活方式：「信号模式与市况」页切换到「和乘幂」Tab（写 signal.active_model=hexp）。
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import {
  Box,
  Button,
  Card,
  CardContent,
  CardHeader,
  CircularProgress,
  Divider,
  Alert,
  Snackbar,
} from '@mui/material';
import SaveIcon from '@mui/icons-material/Save';
import ConfigForm, { ConfigField, ConfigFormHandle } from '../../components/ConfigForm';
import client from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import SaveGuardDialog from '../../components/SaveGuardDialog';

interface HexpConfigState {
  [key: string]: string | number | boolean;
}

const FIELDS: Record<string, ConfigField[]> = {
  // ── 总开关与周期 ──
  core: [
    { key: 'hexp.enabled', label: '和乘幂总开关', type: 'switch', defaultValue: true,
      description: '关闭后和乘幂不再生产信号（无需切回其他模型）。', suggested: 'true（开）' },
    { key: 'hexp.periods', label: '多周期组合', type: 'text', defaultValue: 'M5,M30,H1,H4,D1',
      description: '逗号分隔任意周期组合；按分钟升序，最小=主执行周期，其余=方向层（含 M30）。', suggested: 'M5,M30,H1,H4,D1' },
    { key: 'hexp.period_minutes', label: '周期→分钟映射', type: 'text', defaultValue: 'M1=1,M5=5,M15=15,M30=30,H1=60,H2=120,H4=240,D1=1440',
      description: '周期字母→分钟数映射，用于排序主/方向层与共振权重归一。', suggested: 'M1=1,M5=5,M15=15,M30=30,H1=60,H2=120,H4=240,D1=1440' },
    { key: 'hexp.primary_period', label: '主执行周期', type: 'select', options: [
        { label: 'M5（量化主周期）', value: 'M5' },
        { label: 'M15', value: 'M15' },
        { label: 'H1', value: 'H1' },
      ], defaultValue: 'M5',
      description: '量化交易主周期：入场/止损/震荡策略主执行。', suggested: 'M5' },
    { key: 'hexp.direction_min_score', label: '方向裁定门槛', type: 'number', defaultValue: 0.20, min: 0.05, max: 0.5, step: 0.01,
      description: '|buy-sell|<0.01 且 max<此值 → NO_TRADE（信号太弱）。', suggested: '0.20' },
  ],

  // ── 幂指数 k 自适应（和乘幂核心）──
  k: [
    { key: 'hexp.k.base', label: 'k 基准值', type: 'number', defaultValue: 1.5, min: 0.5, max: 3, step: 0.1,
      description: '幂指数基准（中性）；k=1 退化为普通加权和。', suggested: '1.5' },
    { key: 'hexp.k.min', label: 'k 下限', type: 'number', defaultValue: 0.5, min: 0.2, max: 1, step: 0.05,
      description: '凹收敛极限：k<1 要求多因子共识，抑制单因子假突破。', suggested: '0.5' },
    { key: 'hexp.k.max', label: 'k 上限', type: 'number', defaultValue: 3.0, min: 1.5, max: 5, step: 0.1,
      description: '凸增强极限：k>1 强因子主导，趋势中不被超买超卖拖后腿。', suggested: '3.0' },
    { key: 'hexp.k.alpha', label: 'ADX 影响权重', type: 'number', defaultValue: 0.8, min: 0, max: 2, step: 0.05,
      description: 'k 自适应公式中 ADX 强度系数。', suggested: '0.8' },
    { key: 'hexp.k.beta', label: 'BBW 影响权重', type: 'number', defaultValue: 0.4, min: 0, max: 1, step: 0.05,
      description: 'k 自适应公式中带宽分位系数。', suggested: '0.4' },
    { key: 'hexp.k.state_trend', label: '趋势市 k 基准', type: 'number', defaultValue: 2.0, min: 1, max: 3, step: 0.1,
      description: 'TREND 状态：凸增强，最强因子主导。', suggested: '2.0' },
    { key: 'hexp.k.state_range', label: '震荡市 k 基准', type: 'number', defaultValue: 0.65, min: 0.3, max: 1, step: 0.05,
      description: 'RANGE 状态：凹收敛，要求共识。', suggested: '0.65' },
    { key: 'hexp.k.state_transition', label: '转换中 k 基准', type: 'number', defaultValue: 1.0, min: 0.5, max: 2, step: 0.1,
      description: 'TRANSITION 状态：平衡中性。', suggested: '1.0' },
    { key: 'hexp.k.state_fade', label: '趋势衰竭 k 基准', type: 'number', defaultValue: 2.5, min: 1, max: 3.5, step: 0.1,
      description: 'TREND_FADE：强凸，只信最强因子。', suggested: '2.5' },
  ],

  // ── 因子权重（运行时归一）──
  factor: [
    { key: 'hexp.factor.adx_weight', label: 'ADX 权重', type: 'number', defaultValue: 25, min: 0, max: 100, step: 1,
      description: '趋势强度因子（方向无关，DI 定方向）。', suggested: '25' },
    { key: 'hexp.factor.er_weight', label: 'ER 权重', type: 'number', defaultValue: 25, min: 0, max: 100, step: 1,
      description: 'Kaufman 效率比（直线程度，抗震荡欺骗）。', suggested: '25' },
    { key: 'hexp.factor.ma_weight', label: 'MA 权重', type: 'number', defaultValue: 20, min: 0, max: 100, step: 1,
      description: 'EMA20/50/100 三腿排列 + EMA20 回归斜率。', suggested: '20' },
    { key: 'hexp.factor.bbw_weight', label: 'BBW 权重', type: 'number', defaultValue: 15, min: 0, max: 100, step: 1,
      description: '布林带宽 120 根分位数（挤压→扩张）。', suggested: '15' },
    { key: 'hexp.factor.hurst_weight', label: 'Hurst 权重', type: 'number', defaultValue: 10, min: 0, max: 100, step: 1,
      description: 'R/S 长期记忆（>0.5 趋势持续 / <0.5 均值回归）。', suggested: '10' },
    { key: 'hexp.factor.rsi_weight', label: 'RSI 权重', type: 'number', defaultValue: 5, min: 0, max: 100, step: 1,
      description: '超买超卖辅助（趋势/震荡双模式）。', suggested: '5' },
    { key: 'hexp.factor.mm_weight', label: '微结构动量权重', type: 'number', defaultValue: 15, min: 0, max: 100, step: 1,
      description: 'M1 幂律加权动量（前置预警，第 7 因子）。', suggested: '15' },
  ],

  // ── 因子参数 ──
  param: [
    { key: 'hexp.adx.min', label: 'ADX 归一下界', type: 'number', defaultValue: 15, min: 5, max: 30, step: 1,
      description: 'ADX≤此值归一为 0（无趋势）。', suggested: '15' },
    { key: 'hexp.adx.max', label: 'ADX 归一上界', type: 'number', defaultValue: 35, min: 25, max: 60, step: 1,
      description: 'ADX≥此值归一为 1（强趋势）。', suggested: '35' },
    { key: 'hexp.er.period', label: 'ER 周期', type: 'number', defaultValue: 20, min: 10, max: 60, step: 1,
      description: 'Kaufman 效率比回看窗口（bars）。', suggested: '20' },
    { key: 'hexp.er.min', label: 'ER 归一下界', type: 'number', defaultValue: 0.10, min: 0.01, max: 0.3, step: 0.01,
      description: 'ER≤此值归一为 0（震荡）。', suggested: '0.10' },
    { key: 'hexp.er.max', label: 'ER 归一上界', type: 'number', defaultValue: 0.40, min: 0.2, max: 0.8, step: 0.01,
      description: 'ER≥此值归一为 1（极强单边）。', suggested: '0.40' },
    { key: 'hexp.ma.ema_fast', label: 'EMA 短周期', type: 'number', defaultValue: 20, min: 5, max: 60, step: 1,
      description: 'EMA 排列最短腿。', suggested: '20' },
    { key: 'hexp.ma.ema_mid', label: 'EMA 中周期', type: 'number', defaultValue: 50, min: 10, max: 120, step: 1,
      description: 'EMA 排列中腿。', suggested: '50' },
    { key: 'hexp.ma.ema_long', label: 'EMA 长周期', type: 'number', defaultValue: 100, min: 20, max: 240, step: 1,
      description: 'EMA 排列最长腿。', suggested: '100' },
    { key: 'hexp.ma.align_score', label: '排列对齐分', type: 'number', defaultValue: 50, min: 0, max: 60, step: 1,
      description: 'EMA 三腿全排列加/减分（±）。', suggested: '50' },
    { key: 'hexp.ma.slope_norm_bp', label: 'EMA20 斜率基准(bp)', type: 'number', defaultValue: 3.0, min: 1, max: 30, step: 0.5,
      description: '斜率绝对值÷此值→斜率分（1bp=0.01%）。', suggested: '3.0' },
    { key: 'hexp.ma.slope_bars', label: '斜率回归根数', type: 'number', defaultValue: 10, min: 3, max: 30, step: 1,
      description: 'EMA20 线性回归窗口。', suggested: '10' },
    { key: 'hexp.bbw.window', label: 'BBW 分位窗口', type: 'number', defaultValue: 120, min: 30, max: 300, step: 10,
      description: '当前 BBW 在最近 N 根中的分位。', suggested: '120' },
    { key: 'hexp.bbw.boll_period', label: 'BBW 布林周期', type: 'number', defaultValue: 20, min: 10, max: 60, step: 1,
      description: '带宽计算所用布林周期。', suggested: '20' },
    { key: 'hexp.bbw.boll_std', label: 'BBW 布林标准差', type: 'number', defaultValue: 2.0, min: 1, max: 4, step: 0.1,
      description: '带宽计算标准差倍数。', suggested: '2.0' },
    { key: 'hexp.hurst.min', label: 'Hurst 归一下界', type: 'number', defaultValue: 0.40, min: 0.3, max: 0.5, step: 0.01,
      description: '<0.5 偏均值回归。', suggested: '0.40' },
    { key: 'hexp.hurst.max', label: 'Hurst 归一上界', type: 'number', defaultValue: 0.60, min: 0.5, max: 0.7, step: 0.01,
      description: '>0.5 偏持久趋势。', suggested: '0.60' },
    { key: 'hexp.hurst.max_lag', label: 'Hurst 滞后窗', type: 'number', defaultValue: 32, min: 10, max: 60, step: 1,
      description: 'R/S 估计最大滞后（bars）。', suggested: '32' },
  ],

  // ── 状态机（迟滞）──
  state: [
    { key: 'hexp.state.enter_score', label: '进入趋势门槛', type: 'number', defaultValue: 60, min: 40, max: 90, step: 1,
      description: 'TrendScore≥此值且方向明确→进入趋势态（迟滞上沿）。', suggested: '60' },
    { key: 'hexp.state.exit_score', label: '退出趋势门槛', type: 'number', defaultValue: 40, min: 10, max: 60, step: 1,
      description: 'TrendScore<此值→退出趋势（迟滞下沿）。', suggested: '40' },
    { key: 'hexp.state.confirm_bars', label: '确认根数', type: 'number', defaultValue: 1, min: 1, max: 5, step: 1,
      description: '状态切换需连续确认的 bar 数（防抖）。', suggested: '1' },
  ],

  // ── 微结构动量（新信号源）──
  mm: [
    { key: 'hexp.mm.alpha', label: '幂律衰减指数 α', type: 'number', defaultValue: 0.5, min: 0.1, max: 3, step: 0.1,
      description: '越大衰减越快越敏捷（0.5 平滑 / 1.0 平衡 / 2.0 敏捷）。', suggested: '0.5' },
    { key: 'hexp.mm.window', label: '动量回看窗口', type: 'number', defaultValue: 20, min: 5, max: 60, step: 1,
      description: 'M1 级回看根数。', suggested: '20' },
    { key: 'hexp.mm.period', label: '微结构数据周期', type: 'select', options: [
        { label: 'M1', value: 'M1' },
        { label: 'M5', value: 'M5' },
      ], defaultValue: 'M1',
      description: '幂律动量所用 K 线周期（默认 M1）。', suggested: 'M1' },
    { key: 'hexp.mm.scale', label: 'MM 归一尺度', type: 'number', defaultValue: 0.002, min: 0.0005, max: 0.01, step: 0.0005,
      description: 'tanh 归一分母（对数收益量级）。', suggested: '0.002' },
    { key: 'hexp.mm.accel_k_boost', label: '共振加速 k 增量', type: 'number', defaultValue: 0.3, min: 0, max: 1, step: 0.05,
      description: 'MM 与大因子共振时 k 临时提升量。', suggested: '0.3' },
    { key: 'hexp.mm.accel_threshold', label: '共振加速阈值', type: 'number', defaultValue: 0.7, min: 0.3, max: 0.95, step: 0.05,
      description: '|f_mm| 超此值且大因子>0.5 才触发加速。', suggested: '0.7' },
  ],

  // ── 共振矩阵（方向裁决）──
  mtf: [
    { key: 'hexp.mtf.weight_D1', label: '共振权重 D1', type: 'number', defaultValue: 0.25, min: 0, max: 1, step: 0.05,
      description: '日线方向裁决权重。', suggested: '0.25' },
    { key: 'hexp.mtf.weight_H4', label: '共振权重 H4', type: 'number', defaultValue: 0.35, min: 0, max: 1, step: 0.05,
      description: '4 小时方向裁决权重（主方向层）。', suggested: '0.35' },
    { key: 'hexp.mtf.weight_H1', label: '共振权重 H1', type: 'number', defaultValue: 0.25, min: 0, max: 1, step: 0.05,
      description: '1 小时方向裁决权重。', suggested: '0.25' },
    { key: 'hexp.mtf.weight_M30', label: '共振权重 M30', type: 'number', defaultValue: 0.15, min: 0, max: 1, step: 0.05,
      description: '30 分钟方向裁决权重（选配）。', suggested: '0.15' },
    { key: 'hexp.resonance.tailwind_bonus', label: '顺风加成系数(方案B对称)', type: 'number', defaultValue: 0.0, min: 0, max: 0.5, step: 0.01,
      description: '方案B对称降分：顺风温和加成系数，默认0=完全对称(不虚涨推闸)；逆风改由 penalty 折扣、不再硬封。', suggested: '0.0' },
    { key: 'hexp.resonance.penalty', label: '逆风折扣系数', type: 'number', defaultValue: 0.15, min: 0, max: 0.5, step: 0.01,
      description: 'verdict 与信号反向时 hp ×(1-|verdict|×此值)。', suggested: '0.15' },
    { key: 'hexp.resonance.pullback_penalty', label: '逆风/回踩单降分系数', type: 'number', defaultValue: 1.0, min: 0.1, max: 1, step: 0.05,
      description: '1.0=不额外罚；<1.0=逆风/回踩单综合评分(total)乘性下压，使分级更难达到最低可下单门槛。仅降分不硬封。',
      suggested: '1.0' },
  ],

  // ── 评分卡（S/A/B/C/红灯分级）──
  scorecard: [
    { key: 'hexp.scorecard.weight_resonance', label: '评分-共振权重', type: 'number', defaultValue: 12, min: 0, max: 100, step: 1,
      description: '多周期共振维权重。', suggested: '12' },
    { key: 'hexp.scorecard.weight_state', label: '评分-状态权重', type: 'number', defaultValue: 27, min: 0, max: 100, step: 1,
      description: '和乘幂强度维权重（hp_100）。', suggested: '27' },
    { key: 'hexp.scorecard.weight_entry', label: '评分-入场权重', type: 'number', defaultValue: 26, min: 0, max: 100, step: 1,
      description: '入场技术维（贴 EMA20 回踩位 + 实体占比）。', suggested: '26' },
    { key: 'hexp.scorecard.weight_position', label: '评分-位置权重', type: 'number', defaultValue: 15, min: 0, max: 100, step: 1,
      description: '距 Donchian(20) 反向边界空间。', suggested: '15' },
    { key: 'hexp.scorecard.weight_vol', label: '评分-波动权重', type: 'number', defaultValue: 10, min: 0, max: 100, step: 1,
      description: 'ATR 分位 30-70% 最佳。', suggested: '10' },
    { key: 'hexp.scorecard.weight_session', label: '评分-时段权重', type: 'number', defaultValue: 10, min: 0, max: 100, step: 1,
      description: '伦敦/纽约重叠满分，亚盘减半。', suggested: '10' },
    { key: 'hexp.scorecard.pass_threshold', label: '放行门槛', type: 'number', defaultValue: 50, min: 10, max: 90, step: 1,
      description: '总分<此值 → 红灯禁止开仓。', suggested: '50' },
    { key: 'hexp.scorecard.b_threshold', label: 'B 级门槛', type: 'number', defaultValue: 52, min: 30, max: 90, step: 1,
      description: '总分≥此值评 B 级。', suggested: '52' },
    { key: 'hexp.scorecard.a_threshold', label: 'A/S 级门槛', type: 'number', defaultValue: 75, min: 50, max: 95, step: 1,
      description: '总分≥此值评 A 级（再叠加 hp 条件升 S）。', suggested: '75' },
    { key: 'hexp.scorecard.s_hp_min', label: 'S 级 hp 下限', type: 'number', defaultValue: 60, min: 30, max: 90, step: 1,
      description: '升 S 所需的和乘幂强度下限。', suggested: '60' },
    { key: 'hexp.scorecard.hp_floor', label: 'hp 地板', type: 'number', defaultValue: 30, min: 10, max: 60, step: 1,
      description: 'hp_100 低于此值一律红灯。', suggested: '30' },
    { key: 'hexp.min_grade', label: '最低可下单评级', type: 'select', defaultValue: 'C', suggested: 'A',
      options: [
        { label: 'S（特优，信号极少）', value: 'S' },
        { label: 'A（精确，信号约 -93%）', value: 'A' },
        { label: 'B（均衡）', value: 'B' },
        { label: 'C（宽松，含观察级）', value: 'C' },
      ],
      description: '低于此评级（RED 永远禁交易）的信号只落库观测、不产交易方向。S/A 精确但信号稀疏，C 宽松最频繁。保存时自动同步「部署意图档位」，避免被自愈校准拉回旧档。' },
  ],

  // ── 极值闸门（2026-08-13 治「高位做多/低位做空」+ 动量感知升级 + 反转护栏）──
  extreme: [
    { key: 'hexp.extreme.high_pct', label: '高位分位阈值', type: 'number', defaultValue: 0.85, min: 0.5, max: 0.99, step: 0.01,
      description: '价格相对 Donchian(look) 上轨分位 ≥ 此值 → 高位区（禁止顺势 BUY 追单，除非动量仍同向）。', suggested: '0.85' },
    { key: 'hexp.extreme.low_pct', label: '低位分位阈值', type: 'number', defaultValue: 0.15, min: 0.01, max: 0.5, step: 0.01,
      description: '价格相对 Donchian(look) 下轨分位 ≤ 此值 → 低位区（禁止顺势 SELL 追单，除非动量仍同向）。', suggested: '0.15' },
    { key: 'hexp.extreme.donchian_look', label: 'Donchian 回看根数', type: 'number', defaultValue: 20, min: 5, max: 60, step: 1,
      description: '极值分位计算的 Donchian 通道回看窗口（bars）。', suggested: '20' },
    { key: 'hexp.extreme.mm_retreat_enabled', label: '极值动量感知闸门', type: 'switch', defaultValue: true,
      description: '开启后：极值区仅当 M1 动量回撤(mm_aligned<mm_retreat_min)才封单；动量仍朝原方向→允许极值追单。', suggested: '开启' },
    { key: 'hexp.extreme.mm_retreat_min', label: '极值追单动量下限', type: 'number', defaultValue: 0.20, min: 0, max: 1, step: 0.05,
      description: 'mm_aligned ≥ 此值才放行极值追单；低于则需回撤(reason=hexp_extreme_guard)。', suggested: '0.20' },
    { key: 'hexp.extreme.support_lookback', label: '回踩支撑回看根数', type: 'number', defaultValue: 20, min: 5, max: 60, step: 1,
      description: '取近期摆动低(BUY)/高(SELL)的回看窗口；现价落入 ±support_atr×ATR 即标记回踩支撑。', suggested: '20' },
    { key: 'hexp.extreme.support_atr', label: '回踩支撑 ATR 倍数', type: 'number', defaultValue: 1.0, min: 0.1, max: 5, step: 0.1,
      description: '回踩支撑诊断的 ATR 容差倍数（±support_atr×ATR）。', suggested: '1.0' },
    { key: 'hexp.extreme.chase_sl_mult', label: '极值追单 SL 收紧倍数', type: 'number', defaultValue: 0.7, min: 0.3, max: 1.5, step: 0.05,
      description: 'extreme_chase 时 SL 的 ATR 倍数 × 此值（默认 0.7），TP 不变 → R:R 改善。', suggested: '0.7' },
    { key: 'hexp.extreme.reversal_enabled', label: '极值反转护栏', type: 'switch', defaultValue: true,
      description: '顶/底极值区 + 动量减弱且反向 + 长影线 → 拦原趋势延续单（防极值区逆势陷阱）。', suggested: '开启' },
    { key: 'hexp.extreme.wick_min', label: '反转长影线阈值', type: 'number', defaultValue: 0.60, min: 0.1, max: 0.95, step: 0.05,
      description: '影线占比 ≥ 此值视为长影线（reversal_enabled 判据之一）。', suggested: '0.60' },
    { key: 'hexp.extreme.reversal_sl_atr_mult', label: '反转护栏 SL 倍数', type: 'number', defaultValue: 0.5, min: 0.2, max: 1.5, step: 0.05,
      description: '反转护栏触发时使用的 SL ATR 倍数（偏紧）。', suggested: '0.5' },
    // ── 方案 B (2026-08-19) zone 硬闸门：与 extreme 互补、串联 ──
    { key: 'hexp.zone.hard_block_enabled', label: '逆结构位硬闸门', type: 'switch', defaultValue: false,
      description: '开启后：现价已显著越过方向对齐结构位(BUY在支撑上方/SELL在阻力下方)的单直接 NO_TRADE，把「逆结构位开仓」挡在门外。与 extreme(极值接刀)互补。', suggested: '关闭' },
    { key: 'hexp.zone.hard_block_atr_mult', label: '逆结构位 ATR 容差', type: 'number', defaultValue: 0.3, min: 0.05, max: 2, step: 0.05,
      description: '判定「显著越过」的 ATR 倍数阈值：价格偏离结构位 > 此倍数×ATR 才算逆结构位而被封。越小越严。', suggested: '0.3' },
    { key: 'hexp.extreme.k_extreme', label: '极值 k 补充阈值', type: 'number', defaultValue: 1.8, min: 1.2, max: 3, step: 0.05,
      description: 'k(由 ADX/BBW 驱动的剧烈偏离指标) > 此值 且 pos 已接近极值侧才视为真极值追单。单边行情拉宽通道后 pos 封顶 1.0 无法区分"刚突破 vs 严重超买"，用 k 作补充。调高→更少拦截（本单 k=1.98 时需 ≥2.3 才触发）。', suggested: '1.8' },
    { key: 'hexp.extreme.k_pos_high', label: 'k 高位侧分位', type: 'number', defaultValue: 0.7, min: 0.5, max: 0.95, step: 0.01,
      description: 'k 补充触发的 BUY 侧分位下限：pos>此值才与 k 联动判极值，避免通道中部正常回调被误拦。', suggested: '0.7' },
    { key: 'hexp.extreme.k_pos_low', label: 'k 低位侧分位', type: 'number', defaultValue: 0.3, min: 0.05, max: 0.5, step: 0.01,
      description: 'k 补充触发的 SELL 侧分位上限：pos<此值才与 k 联动判极值。', suggested: '0.3' },
    { key: 'hexp.momentum_drain_enabled', label: '动量枯竭保护', type: 'switch', defaultValue: true,
      description: '开启后：pos 高位 + 趋势质量差(er 低) + 微动量枯竭(mm 近 0) 三条件齐 → 拦原趋势延续单（高位接刀/追顶）。独立于极值护栏，真趋势(er>0.22)不受影响。', suggested: '开启' },
    { key: 'hexp.momentum_drain_hi', label: '枯竭保护高位分位', type: 'number', defaultValue: 0.65, min: 0.5, max: 0.95, step: 0.01,
      description: '动量枯竭保护的 BUY 侧高位判据：pos>此值才判定"高位"。SELL 侧用 pos<(1-此值)。', suggested: '0.65' },
    { key: 'hexp.momentum_drain_er', label: '枯竭保护 ER 阈值', type: 'number', defaultValue: 0.20, min: 0.05, max: 0.5, step: 0.01,
      description: 'Kaufman 效率比(er)<此值视为"趋势质量差/来回震荡"。er 越低越该拦。', suggested: '0.20' },
    { key: 'hexp.momentum_drain_mm', label: '枯竭保护动量阈值', type: 'number', defaultValue: 0.15, min: 0.01, max: 0.5, step: 0.01,
      description: '微动量对齐度(mm_aligned)<此值视为"动能枯竭"。与 er 低 + 高位 三者齐才拦截。', suggested: '0.15' },
    { key: 'hexp.momentum_flip_enabled', label: '动量方向否决', type: 'switch', defaultValue: true,
      description: '开启后：动量明确反向时拦逆动量单（BUY 而 mm<0、SELL 而 mm>0），不依赖 pos/趋势结构。让"动量转负即不再追多/追空"，比动量枯竭更敏捷。', suggested: '开启' },
    { key: 'hexp.momentum_flip_mm', label: '动量反向阈值', type: 'number', defaultValue: 0.04, min: 0.01, max: 0.2, step: 0.005,
      description: '|f_mm|≥此值视为动量明确反转：BUY 单 mm<-此值 拦、SELL 单 mm>此值 拦。越小越敏感（更拦转跌追单，但也可能误杀小回调）。', suggested: '0.04' },
    { key: 'hexp.reverse_candidate_enabled', label: '反向单观测开关', type: 'switch', defaultValue: true,
      description: '开启后：momentum_flip 判动量反向且处于高位/低位时，记录反向候选(dir/pos/er/mm)落库供对照评估。零实盘影响。', suggested: '开启' },
    { key: 'hexp.reverse_candidate_hi', label: '反向候选高位分位', type: 'number', defaultValue: 0.7, min: 0.5, max: 0.95, step: 0.01,
      description: 'BUY 被拦→SELL 候选 的高位分位阈值（pos>此值）。', suggested: '0.7' },
    { key: 'hexp.reverse_candidate_lo', label: '反向候选低位分位', type: 'number', defaultValue: 0.3, min: 0.05, max: 0.5, step: 0.01,
      description: 'SELL 被拦→BUY 候选 的低位分位阈值（pos<此值）。', suggested: '0.3' },
  ],

  // ── 执行参数（桥消费 ai_sl_mult / ai_tp_mult / lot）──
  exec: [
    { key: 'hexp.exec.sl_atr_mult', label: 'SL ATR 倍数', type: 'number', defaultValue: 2.0, min: 0.5, max: 5, step: 0.1,
      description: '止损距离 = 此倍数 × ATR。', suggested: '2.0' },
    { key: 'hexp.exec.rr_min', label: '最低盈亏比 R:R', type: 'number', defaultValue: 1.5, min: 1, max: 3, step: 0.1,
      description: 'TP = SL 距离 × 此值。', suggested: '1.5' },
    { key: 'hexp.exec.lot_mult', label: '仓位倍数', type: 'number', defaultValue: 1.0, min: 0.1, max: 5, step: 0.1,
      description: '下单手数倍率（再乘分级系数）。', suggested: '1.0' },
    { key: 'hexp.exec.grade_lot_s', label: 'S 级仓位系数', type: 'number', defaultValue: 1.2, min: 0.5, max: 3, step: 0.1,
      description: 'S 级信号手数乘数。', suggested: '1.2' },
    { key: 'hexp.exec.grade_lot_a', label: 'A 级仓位系数', type: 'number', defaultValue: 1.0, min: 0.5, max: 3, step: 0.1,
      description: 'A 级信号手数乘数。', suggested: '1.0' },
    { key: 'hexp.exec.grade_lot_b', label: 'B 级仓位系数', type: 'number', defaultValue: 0.5, min: 0.1, max: 2, step: 0.1,
      description: 'B 级信号手数乘数。', suggested: '0.5' },
    { key: 'hexp.exec.grade_lot_c', label: 'C 级仓位系数', type: 'number', defaultValue: 0.5, min: 0.1, max: 2, step: 0.1,
      description: 'C 级信号手数乘数。', suggested: '0.5' },
    { key: 'hexp.exec.transition_lot_mult', label: '转换态降仓系数', type: 'number', defaultValue: 0.5, min: 0.1, max: 1, step: 0.05,
      description: '任一周期 TRANSITION（犹豫带）时仓位再乘此值。', suggested: '0.5' },
    { key: 'hexp.exec.reversal_lot_mult', label: '反转态降仓系数', type: 'number', defaultValue: 0.5, min: 0.1, max: 1, step: 0.05,
      description: '任一周期发生 TREND_UP↔TREND_DOWN 干净反转时仓位再乘此值。与转换态系数取更谨慎者（不叠乘）。',
      suggested: '0.5' },
    { key: 'hexp.exec.reversal_hold_bars', label: '反转态持有棒数', type: 'number', defaultValue: 3, min: 0, max: 20, step: 1,
      description: '反转是「态」不是瞬时事件：自触发起持有 N 根主周期棒内一律减仓。0=仅触发当次（等于几乎不减仓）。',
      suggested: '3' },
    { key: 'hexp.exec.reversal_include_primary', label: '主周期方向切换计入反转', type: 'switch', defaultValue: false,
      description: '是否把主执行周期(M5)的方向切换也计为反转。默认关：M5 切换过于频繁，开启会让减仓常驻（等价于直接砍半仓位）。',
      suggested: '关闭' },
    { key: 'hexp.exec.extreme_lot_mult', label: '极值区降仓系数', type: 'number', defaultValue: 0.5, min: 0.1, max: 1, step: 0.05,
      description: '处于 Donchian 极值区且未被护栏封单的放行单，手数再乘此系数（与转换/反转态取更谨慎者，不叠乘）。',
      suggested: '0.5' },
    { key: 'hexp.exec.vol_scale_enabled', label: '波动率缩放手数', type: 'switch', defaultValue: true,
      description: '开启后 ATR 相对常态时基础手数随行情缩放：波动放大→降仓（防满仓接大波动），波动收窄→不超配。',
      suggested: '开启' },
    { key: 'hexp.exec.vol_scale_atr_ref', label: '波动率 ATR 基准', type: 'number', defaultValue: 7.0, min: 1, max: 30, step: 0.5,
      description: '常态 ATR 基准（波动率中性点），实际 ATR 相对它的比值驱动缩放。',
      suggested: '7.0' },
    { key: 'hexp.exec.vol_scale_min', label: '波动缩放下限', type: 'number', defaultValue: 0.5, min: 0.1, max: 1, step: 0.05,
      description: '波动放大时最低缩到该系数（防止满仓接大波动）。',
      suggested: '0.5' },
    { key: 'hexp.exec.vol_scale_max', label: '波动缩放上限', type: 'number', defaultValue: 1.0, min: 0.5, max: 2, step: 0.05,
      description: '波动收窄时最高放大到该系数（绝不超配基础手数）。',
      suggested: '1.0' },
  ],
};

const GROUP_SUMMARY: Record<string, string> = {
  core: '总开关 + 多周期组合（最小=主执行 M5，其余=方向层）',
  k: '和乘幂核心：幂指数 k 随市况自适应（趋势凸增强 / 震荡凹收敛）',
  factor: '六因子 + 微结构动量权重（运行时自动归一）',
  param: '因子归一区间与参数（ADX/ER/EMA/BBW/Hurst）',
  state: '迟滞状态机（进入/退出门槛 + 确认根数，防抖动）',
  mm: '新信号源：M1 微结构动量幂律（前置预警 + 共振加速）',
  mtf: '多周期共振方向裁决（主执行周期权重=0 不自我裁决）',
  scorecard: '6 维评分卡 + S/A/B/C/红灯分级',
  extreme: '极值闸门（Donchian 分位封顺势追单 + 动量感知 + 回踩支撑 + 极值反转护栏）',
  exec: '执行参数（SL×ATR / R:R / 分级仓位系数 + 转换态·反转态减仓，桥消费）',
};

const GROUP_ORDER = ['core', 'k', 'factor', 'param', 'state', 'mm', 'mtf', 'scorecard', 'extreme', 'exec'];

const GROUP_TITLES: Record<string, string> = {
  core: 'C 总开关与周期 (Core)',
  k: 'K 幂指数自适应 (Power k)',
  factor: 'F 因子权重 (Factor)',
  param: 'N 因子参数 (Normalization)',
  state: 'S 迟滞状态机 (State)',
  mm: 'M 微结构动量 (Microstructure)',
  mtf: 'R 共振矩阵 (Resonance)',
  scorecard: 'G 评分卡 (Grade)',
  extreme: 'X 极值闸门 (Extreme)',
  exec: 'E 执行参数 (Execution)',
};

const GROUP_COLORS: Record<string, string> = {
  core: '#8b5cf6',
  k: '#a855f7',
  factor: '#22d3ee',
  param: '#34d399',
  state: '#fbbf24',
  mm: '#f472b6',
  mtf: '#f59e0b',
  scorecard: '#c084fc',
  extreme: '#fb7185',
  exec: '#60a5fa',
};

const buildHexpFields = (): ConfigField[] => {
  const out: ConfigField[] = [];
  for (const g of GROUP_ORDER) {
    out.push({
      key: `sec_${g}`,
      label: GROUP_TITLES[g],
      type: 'section',
      color: GROUP_COLORS[g],
      description: GROUP_SUMMARY[g],
    });
    out.push(...FIELDS[g]);
  }
  return out;
};

interface HexpConfigProps {
  /** 内嵌模式：在「信号模式与市况」Tab 中渲染时去掉外层 Card 包装。 */
  embedded?: boolean;
}

export default function HexpConfig({ embedded = false }: HexpConfigProps) {
  const [config, setConfig] = useState<HexpConfigState>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<{ msg: string; severity: 'success' | 'error' } | null>(null);
  const [guardOpen, setGuardOpen] = useState(false);
  // 一次性密码鉴权凭证：仅当守卫弹窗密码校验通过后才置 true，保存后立即消费
  const pwVerified = useRef(false);
  // 精确指向配置表单的命令式提交句柄，用于守卫确认后触发提交（避免全局 document.querySelector('form') 歧义）
  const formRef = useRef<ConfigFormHandle>(null);
  const { user } = useAuth();

  const groupedFields = useMemo(() => buildHexpFields(), []);

  useEffect(() => {
    (async () => {
      try {
        const resp = await client.get<HexpConfigState>('/api/v1/hexp/config');
        const body = resp.data as {
          code?: number;
          data?: HexpConfigState;
          message?: string;
        };
        const d = body && body.data ? body.data : (resp.data as HexpConfigState);
        setConfig(d || {});
      } catch (e) {
        console.error('加载和乘幂配置失败', e);
        setToast({ msg: '加载失败，请重试', severity: 'error' });
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  const handleSave = async (values: Record<string, unknown>) => {
    // 密码鉴权闸门：守卫弹窗校验通过后才允许真正写入
    if (!pwVerified.current) {
      setToast({ msg: '请先通过密码鉴权再保存', severity: 'error' });
      return;
    }
    pwVerified.current = false; // 消费一次性凭证
    setSaving(true);
    const updates = Object.entries(values).map(([config_key, value]) => {
      let v = value;
      if (typeof value === 'boolean') v = value ? 'true' : 'false';
      else if (typeof value === 'number') v = String(value);
      return { config_key, value: v as string };
    });
    // 2026-08-21 修复「可下单等级保存后刷新又复原」：
    // hexp.min_grade 是引擎实际档位，但 web 自愈校准 calibrate_min_grade 会把
    // min_grade 强制锚定回「部署意图」hexp.min_grade_intended（默认 B）。若只改
    // min_grade 不同步 intended，一次诊断自愈就把用户选档拉回 B → "保存后复原"。
    // 故保存 min_grade 时同步把 min_grade_intended 设为同一值，使部署意图跟随用户选择。
    const mg = updates.find((u) => u.config_key === 'hexp.min_grade');
    if (mg && !updates.some((u) => u.config_key === 'hexp.min_grade_intended')) {
      updates.push({ config_key: 'hexp.min_grade_intended', value: mg.value });
    }
    try {
      await client.put('/api/v1/hexp/config', { updates });
      setConfig(values as HexpConfigState);
      setToast({ msg: '和乘幂配置已保存（引擎热生效）', severity: 'success' });
    } catch (e) {
      console.error('保存和乘幂配置失败', e);
      setToast({ msg: '保存失败，请重试', severity: 'error' });
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <Box display="flex" justifyContent="center" alignItems="center" minHeight={200}>
        <CircularProgress />
      </Box>
    );
  }

  const formBody = (
    <>
      <Alert severity="info" sx={{ mb: 2 }}>
        所有改动经配置中心(PG↔Redis <code>hcm:config:v2</code>)热写入，引擎每循环热读，保存后通常无需重启桥/信号塔。
      </Alert>
      <ConfigForm
        ref={formRef}
        fields={groupedFields}
        initialValues={config}
        onSubmit={handleSave}
        grouped
        groupColumns={2}
        memberColumns={2}
        hideSubmit
      />
    </>
  );

  const saveButton = (
    <Button
      variant="contained"
      startIcon={<SaveIcon />}
      disabled={saving}
      onClick={() => setGuardOpen(true)}
    >
      {saving ? '保存中…' : '保存配置'}
    </Button>
  );

  return (
    <Box>
      {embedded ? (
        <Box>
          <Box sx={{ display: 'flex', justifyContent: 'flex-end', mb: 1 }}>{saveButton}</Box>
          {formBody}
        </Box>
      ) : (
        <Card>
          <CardHeader
            title="和乘幂信号配置 (Hexp / Harmonic Power-Mean)"
            subheader="独立信号源：HP-Score 广义均值 + k 自适应 + 多周期共振 + M1 微结构动量。悬停任一参数可看「作用」与「建议值」。在「信号模式与市况」页切换激活。"
            action={saveButton}
          />
          <Divider />
          <CardContent>{formBody}</CardContent>
        </Card>
      )}

      {/* ── 保存守卫：二次确认 + 密码鉴权 ── */}
      <SaveGuardDialog
        open={guardOpen}
        title="确认保存和乘幂参数"
        description="即将写入和乘幂（HEXP）配置中心（PG↔Redis）。请确认参数无误，输入登录密码授权保存。"
        username={user?.username}
        onClose={() => setGuardOpen(false)}
        onConfirmed={() => {
          pwVerified.current = true;
          formRef.current?.submit();
        }}
      />

      <Snackbar
        open={!!toast}
        autoHideDuration={3000}
        onClose={() => setToast(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        <Alert severity={toast?.severity ?? 'info'} onClose={() => setToast(null)}>
          {toast?.msg}
        </Alert>
      </Snackbar>
    </Box>
  );
}
