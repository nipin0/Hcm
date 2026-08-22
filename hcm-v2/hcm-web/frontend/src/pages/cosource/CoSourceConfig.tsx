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
  Typography,
} from '@mui/material';
import SaveIcon from '@mui/icons-material/Save';
import ConfigForm, { ConfigField, ConfigFormHandle } from '../../components/ConfigForm';
import client from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import SaveGuardDialog from '../../components/SaveGuardDialog';

interface CoFilterConfig {
  [key: string]: string | number | boolean;
}

const FIELDS: Record<string, ConfigField[]> = {
  // ── G1 校准因子 (Calib) ──
  calib: [
    { key: 'co.calib.pre_trend',  label: '启动前趋势 (PRE_TREND)', type: 'number', defaultValue: 1.0, min: 0.5, max: 1.5, step: 0.05,
      description: 'PRE_TREND 体制下的评分校准乘数。冷启动期恒 1.0，积累≥7天标注后离线校准。', suggested: '1.0（冷启动保持）' },
    { key: 'co.calib.trend',      label: '趋势中 (TREND)',          type: 'number', defaultValue: 1.0, min: 0.5, max: 1.5, step: 0.05,
      description: 'TREND 体制下的评分校准乘数。', suggested: '1.0' },
    { key: 'co.calib.trend_fade', label: '趋势衰退 (TREND_FADE)',   type: 'number', defaultValue: 1.0, min: 0.5, max: 1.5, step: 0.05,
      description: 'TREND_FADE 体制下的评分校准乘数。', suggested: '1.0' },
    { key: 'co.calib.range',      label: '震荡 (RANGE)',            type: 'number', defaultValue: 1.0, min: 0.5, max: 1.5, step: 0.05,
      description: 'RANGE 体制下的评分校准乘数。', suggested: '1.0' },
    { key: 'co.calib.neutral',    label: '中性 (NEUTRAL)',          type: 'number', defaultValue: 1.0, min: 0.5, max: 1.5, step: 0.05,
      description: 'NEUTRAL 体制下的评分校准乘数。', suggested: '1.0' },
    { key: 'co.calib.min_days',   label: '冷启动最少天数',          type: 'number', defaultValue: 7,   min: 3,   max: 30,  step: 1,
      description: '标注天数低于此值跳过自动校准，避免样本不足误校。', suggested: '7' },
  ],

  // ── G2 假信号过滤 (Filter) ──
  filter: [
    { key: 'co.filter.f1_enabled', label: 'F1 周期背离检测', type: 'switch', defaultValue: true,
      description: '价格创新高/低但 MACD 柱未确认→顶/底背离，杀延续单。反转早期信号。', suggested: 'true（开）' },
    { key: 'co.filter.f1_penalty', label: 'F1 背离扣分', type: 'number', defaultValue: 20, min: 0, max: 50, step: 1,
      description: '背离扣分权重（除以 score_scale）。反转初段加重可更早杀延续单，让反转评分更早翻向。', suggested: '8（线上当前为5）' },
    { key: 'co.filter.f2_enabled', label: 'F2 布林收口检测', type: 'switch', defaultValue: true,
      description: '带宽<近20根均值×ratio%→收口，过滤假突破。', suggested: 'true（开）' },
    { key: 'co.filter.f2_ratio', label: 'F2 收口阈值(%)', type: 'number', defaultValue: 50, min: 10, max: 100, step: 5,
      description: '布林收口判定百分比，越小越敏感。', suggested: '50' },
    { key: 'co.filter.f3_enabled', label: 'F3 数据窗口期检测', type: 'switch', defaultValue: true,
      description: '重大数据公布前降分，规避数据行情跳空。', suggested: 'true' },
    { key: 'co.filter.f3_minutes', label: 'F3 窗口分钟数', type: 'number', defaultValue: 30, min: 5, max: 120, step: 5,
      description: '数据公布前多少分钟进入窗口期降分。', suggested: '30' },
    { key: 'co.filter.f3_penalty', label: 'F3 窗口扣分', type: 'number', defaultValue: 25, min: 0, max: 50, step: 1,
      description: '数据窗口期对评分的扣分量。', suggested: '25' },
    { key: 'co.filter.f4_enabled', label: 'F4 超买超卖钝化', type: 'switch', defaultValue: true,
      description: '单边极端 RSI 下取消反向信号，防钝化逆势。', suggested: 'true（开）' },
    { key: 'co.filter.f4_rsi_upper', label: 'F4 RSI 超买阈值', type: 'number', defaultValue: 70, min: 60, max: 90, step: 1,
      description: 'RSI 高于此值判超买（钝化区）。', suggested: '70' },
    { key: 'co.filter.f4_rsi_lower', label: 'F4 RSI 超卖阈值', type: 'number', defaultValue: 30, min: 10, max: 40, step: 1,
      description: 'RSI 低于此值判超卖（钝化区）。', suggested: '30' },
    { key: 'co.filter.f5_enabled', label: 'F5 连续亏损熔断', type: 'switch', defaultValue: true,
      description: '连续 N 笔止损→扣分+触发紧急 AI，防止连续亏损放大。', suggested: 'true（开）' },
    { key: 'co.filter.f5_consecutive', label: 'F5 连续亏损触发次数', type: 'number', defaultValue: 3, min: 2, max: 10, step: 1,
      description: '触发熔断所需的连亏笔数。', suggested: '3' },
    { key: 'co.filter.f5_penalty', label: 'F5 熔断扣分', type: 'number', defaultValue: 30, min: 0, max: 60, step: 1,
      description: '熔断时对评分的扣分量。', suggested: '30' },
    { key: 'co.filter.f6_quality_enabled', label: 'F6 棒质量 / 点差质量闸门', type: 'switch', defaultValue: false,
      description: '灰度开关（默认关）。开启后：最新棒质量分低于阈值、或相对均值点差过宽(spread_q 过高)→作废该方向信号。依赖 A 组已落库的 spread 列。', suggested: 'false（灰度，默认关）' },
    { key: 'co.filter.f6_quality_min', label: 'F6 质量分最低阈值', type: 'number', defaultValue: 0.55, min: 0.3, max: 0.9, step: 0.05,
      description: '最新棒质量分(bar_quality)低于此值→判异常棒，NO_TRADE。', suggested: '0.55' },
    { key: 'co.filter.f6_spread_q_max', label: 'F6 点差质量分上限', type: 'number', defaultValue: 2.0, min: 1.2, max: 5, step: 0.1,
      description: '点差质量分(spread_q=当前点差/近期均值)高于此值→成交成本异常，NO_TRADE。', suggested: '2.0' },
  ],

  // ── G3 自适应入市门槛 (Gate) ──
  gate: [
    { key: 'co.gate.strong.trend', label: '强趋势-门槛', type: 'number', defaultValue: 40, min: 5, max: 90, step: 1,
      description: '强趋势评分门槛(0-100)，越高越挑剔。对齐 DB=30。', suggested: '30' },
    { key: 'co.gate.strong.lot', label: '强趋势-仓位倍率', type: 'number', defaultValue: 1.0, min: 0.3, max: 2.0, step: 0.1,
      description: '强趋势下的仓位倍率。', suggested: '1.0' },
    { key: 'co.gate.strong.sl_atr', label: '强趋势-SL ATR 倍率', type: 'number', defaultValue: 0.5, min: 0.2, max: 1.5, step: 0.1,
      description: '强趋势止损 ATR 倍率，越小止损越紧。', suggested: '0.5' },
    { key: 'co.gate.strong.rr_min', label: '强趋势-最低盈亏比', type: 'number', defaultValue: 2.0, min: 1.0, max: 4.0, step: 0.1,
      description: '强趋势最低 R:R 要求。', suggested: '2.0' },
    { key: 'co.gate.weak.trend', label: '弱趋势-门槛', type: 'number', defaultValue: 50, min: 5, max: 95, step: 1,
      description: '弱趋势评分门槛(0-100)。对齐 DB=25。', suggested: '25' },
    { key: 'co.gate.weak.lot', label: '弱趋势-仓位倍率', type: 'number', defaultValue: 1, min: 0.3, max: 2.0, step: 0.1,
      description: '弱趋势下的仓位倍率。', suggested: '0.8' },
    { key: 'co.gate.weak.sl_atr', label: '弱趋势-SL ATR 倍率', type: 'number', defaultValue: 0.6, min: 0.2, max: 1.5, step: 0.1,
      description: '弱趋势止损 ATR 倍率。', suggested: '0.6' },
    { key: 'co.gate.weak.rr_min', label: '弱趋势-最低盈亏比', type: 'number', defaultValue: 1.5, min: 1.0, max: 4.0, step: 0.1,
      description: '弱趋势最低 R:R 要求。', suggested: '2.5' },
    { key: 'co.gate.shock.trend', label: '突发波动-门槛', type: 'number', defaultValue: 20, min: 10, max: 95, step: 1,
      description: '突发波动评分门槛(0-100)。', suggested: '80' },
    { key: 'co.gate.shock.lot', label: '突发波动-仓位倍率', type: 'number', defaultValue: 0.5, min: 0.1, max: 1.5, step: 0.1,
      description: '突发波动下的仓位倍率（通常更保守）。', suggested: '0.5' },
    { key: 'co.gate.shock.sl_atr', label: '突发波动-SL ATR 倍率', type: 'number', defaultValue: 0.7, min: 0.3, max: 2.0, step: 0.1,
      description: '突发波动止损 ATR 倍率（波动大需更宽）。', suggested: '1.0' },
    { key: 'co.gate.shock.rr_min', label: '突发波动-最低盈亏比', type: 'number', defaultValue: 1.5, min: 1.5, max: 5.0, step: 0.1,
      description: '突发波动最低 R:R 要求（更高更稳）。', suggested: '3.0' },
    { key: 'co.gate.score_scale', label: '分数尺度归一(0–100→0–1)', type: 'number', defaultValue: 100, min: 50, max: 200, step: 5,
      description: '【0–100 归一除数，非权重、非放大倍数】把 PRD 的 0–100 评分尺度归一到引擎内部的 0–1 尺度（pre_score 与所有门槛均为 0–1 尺度，靠此除数统一）。务必保持为 100 的整数倍（默认 100）：若误改为 1，所有门槛会被放大 100 倍→几乎全被拦截；若改为其他非 100 倍数，会整体偏移整套闸门尺度导致交易频率异常。⚠️ 请勿随意调整。', suggested: '100' },
    { key: 'co.gate.adx_strong', label: 'M5 强趋势 ADX', type: 'number', defaultValue: 18, min: 10, max: 50, step: 1,
      description: '[2026-07-31 ADX 收敛] M5 侧"强趋势 ADX"统一为 18：与 scoring.trend_strong_adx_threshold(反向阻断)、scoring.min_adx_for_trade(地板) 一致；H1 慢线专属 regime.trend_strong_adx_threshold=28 为另一概念(时间框架更长、需更高确认，不并入此值)。', suggested: '18' },
    { key: 'co.gate.with_trend.trend', label: '顺H1方向放宽门槛(0-100)', type: 'number', defaultValue: 30, min: 10, max: 60, step: 1,
      description: '[2026-07-31 B] 信号方向等于 H1 bias(顺势高空/低多)时，用此门槛(默认30→0.30)覆盖 M5 体制的 0.40/0.50/0.45，使"顺H1高空"真正能出单；逆H1单不享受豁免。', suggested: '30' },
    { key: 'co.gate.shock.atr_mult', label: '突发波动 ATR 倍率', type: 'number', defaultValue: 2.0, min: 1.2, max: 4.0, step: 0.1,
      description: 'vol_factor≥此值判为突发波动（ATR 相对放大倍数）。', suggested: '2.0' },
    { key: 'co.gate.range.block', label: '震荡/中性市拦截', type: 'switch', defaultValue: false,
      description: '开启后震荡/中性市不产生信号，只在趋势类体制出单。当前 DB=False（关闭，保留震荡市均值回归机会）。', suggested: 'false' },
    { key: 'co.gate.risk.high_offset', label: '高风险门槛偏移', type: 'number', defaultValue: 5, min: 0, max: 30, step: 1,
      description: '高风险时门槛的抬高偏移量(0-100尺度)。', suggested: '10' },
    { key: 'co.gate.risk.med_offset', label: '中风险门槛偏移', type: 'number', defaultValue: 2, min: 0, max: 20, step: 1,
      description: '中风险时门槛的抬高偏移量(0-100尺度)。', suggested: '5' },
    { key: 'co.gate.direction_min_score', label: '方向分离最小分', type: 'number', defaultValue: 0.30, min: 0, max: 1, step: 0.01,
      description: 'buy/sell 评分分差低于此值不触发方向信号，过滤微弱方向单。', suggested: '0.30' },
  ],

  // ── G4 批量 AI (Batch AI) ──
  ai: [
    { key: 'co.ai.c1_enabled', label: 'C1 日线趋势标注', type: 'switch', defaultValue: true,
      description: '工作日 06:00 UTC 用 DeepSeek 标注日线趋势。', suggested: 'true' },
    { key: 'co.ai.c1_cron', label: 'C1 调度 (cron)', type: 'text', defaultValue: '0 6 * * 1-5',
      description: 'C1 的 cron 表达式。', suggested: '0 6 * * 1-5' },
    { key: 'co.ai.c2_enabled', label: 'C2 日内复盘与漏洞', type: 'switch', defaultValue: true,
      description: '工作日 18:00 UTC 日内复盘并找策略漏洞。', suggested: 'true' },
    { key: 'co.ai.c2_cron', label: 'C2 调度 (cron)', type: 'text', defaultValue: '0 18 * * 1-5',
      description: 'C2 的 cron 表达式。', suggested: '0 18 * * 1-5' },
    { key: 'co.ai.c3_enabled', label: 'C3 周日周线对齐', type: 'switch', defaultValue: true,
      description: '周日 12:00 UTC 周线对齐。', suggested: 'true' },
    { key: 'co.ai.c3_cron', label: 'C3 调度 (cron)', type: 'text', defaultValue: '0 12 * * 0',
      description: 'C3 的 cron 表达式。', suggested: '0 12 * * 0' },
    { key: 'co.ai.emergency_enabled', label: '紧急补充调用', type: 'switch', defaultValue: true,
      description: 'F5 熔断或 FORCE_CLOSE 触发时调用 DeepSeek 紧急研判。', suggested: 'true' },
    { key: 'co.ai.emergency_limit', label: '紧急补充调用上限/日', type: 'number', defaultValue: 3, min: 0, max: 10, step: 1,
      description: '每日紧急 AI 调用次数上限，防超额。', suggested: '3' },
    { key: 'co.ai.max_tokens', label: '每次调用 Token 上限', type: 'number', defaultValue: 4096, min: 512, max: 8192, step: 512,
      description: '单次 DeepSeek 调用的 token 上限。', suggested: '4096' },
    { key: 'co.ai.temperature', label: '输出随机性 (temperature)', type: 'number', defaultValue: 0.3, min: 0, max: 1, step: 0.05,
      description: '生成随机性，越低越确定。', suggested: '0.3' },
  ],

  // ── G5 批量调度 (Batch scheduling) ──
  batch: [
    { key: 'co.batch.enabled', label: '启用批量调度', type: 'switch', defaultValue: true,
      description: '开启后按日历/时刻触发批量 AI 研判与下单调度。', suggested: 'true' },
    { key: 'co.batch.calendar_source', label: '交易日历来源', type: 'select', options: [{ label: 'DeepSeek 知识库', value: 'deepseek_knowledge' }, { label: '交易所日历', value: 'exchange_calendar' }], defaultValue: 'deepseek_knowledge',
      description: '判断交易时段/休市的日历来源。', suggested: 'deepseek_knowledge' },
    { key: 'co.batch.call1_time', label: '第一时段 (HH:MM)', type: 'text', defaultValue: '06:00',
      description: '每日第一批量调用时刻（UTC）。', suggested: '06:00' },
    { key: 'co.batch.call2_time', label: '第二时段 (HH:MM)', type: 'text', defaultValue: '18:00',
      description: '每日第二批量调用时刻（UTC）。', suggested: '18:00' },
    { key: 'co.batch.call3_enabled', label: '启用第三时段', type: 'switch', defaultValue: true,
      description: '是否启用第三批量调用窗口。', suggested: 'true' },
    { key: 'co.batch.lookback_days', label: '回看天数', type: 'number', defaultValue: 30, min: 1, max: 365, step: 1,
      description: '批量研判回看的 K 线/样本天数。', suggested: '30' },
    { key: 'co.batch.timeout_sec', label: '单批超时(秒)', type: 'number', defaultValue: 60, min: 5, max: 600, step: 5,
      description: '单批 AI 调用的超时时间，超时跳过防止阻塞。', suggested: '60' },
    { key: 'co.batch.emergency.enabled', label: '批量紧急补充', type: 'switch', defaultValue: true,
      description: '批量场景下 F5 熔断触发时紧急补充研判。', suggested: 'true' },
    { key: 'co.batch.emergency.max_per_day', label: '批量紧急上限/日', type: 'number', defaultValue: 2, min: 0, max: 10, step: 1,
      description: '批量紧急调用每日上限。', suggested: '2' },
    { key: 'co.batch.emergency.cooldown_h', label: '批量紧急冷却(时)', type: 'number', defaultValue: 2, min: 0, max: 24, step: 1,
      description: '批量紧急调用之间的冷却小时数。', suggested: '2' },
  ],

  // ── G6 Optuna 自动调参 ──
  optuna: [
    { key: 'co.optuna.enabled', label: '启用 Optuna 自动调参', type: 'switch', defaultValue: true,
      description: 'TPE sampler 本地寻参，自动搜索最优阈值组合。', suggested: 'true' },
    { key: 'co.optuna.train_days', label: '训练集天数', type: 'number', defaultValue: 60, min: 20, max: 180, step: 5,
      description: 'Optuna 训练集天数。', suggested: '60' },
    { key: 'co.optuna.test_days', label: '测试集天数', type: 'number', defaultValue: 15, min: 5, max: 60, step: 5,
      description: 'Optuna 测试集天数（样本外验证）。', suggested: '15' },
    { key: 'co.optuna.trials', label: '每轮试验次数', type: 'number', defaultValue: 100, min: 20, max: 500, step: 10,
      description: 'Optuna 每轮 trial 次数。', suggested: '100' },
    { key: 'co.optuna.target', label: '优化目标', type: 'select', options: [{ label: '夏普比率', value: 'sharpe_ratio' }, { label: '索提诺比率', value: 'sortino_ratio' }, { label: '卡玛比率', value: 'calmar_ratio' }], defaultValue: 'sharpe_ratio',
      description: 'Optuna 优化的目标指标。', suggested: 'sharpe_ratio' },
    { key: 'co.optuna.max_drawdown_pct', label: '最大回撤约束(%)', type: 'number', defaultValue: 15, min: 5, max: 50, step: 1,
      description: 'Optuna 优化的最大回撤硬约束。', suggested: '15' },
    { key: 'co.optuna.min_trades', label: '最少有效交易', type: 'number', defaultValue: 60, min: 10, max: 200, step: 10,
      description: '样本内最少有效交易数，低于此视为无效 trial。', suggested: '60' },
  ],

  // ── G6 执行增强 / FORCE_CLOSE ──
  exec: [
    { key: 'co.exec.force_close_enabled', label: '启用 FORCE_CLOSE 信号', type: 'switch', defaultValue: false,
      description: 'H1 趋势翻转时由引擎发出强平信号，主动离场。', suggested: 'true' },
    { key: 'co.exec.fc_close_mode', label: '强反转平仓模式', type: 'select', options: [{ label: '全平', value: 'all' }, { label: '减半', value: 'half' }, { label: '走弱减半', value: 'half_on_weakening' }], defaultValue: 'all',
      description: '强反转时平仓方式：全平/减半/走弱减半。', suggested: 'all' },
    { key: 'co.exec.fc_bar_confirm', label: '反转确认 bar 数', type: 'number', defaultValue: 3, min: 1, max: 10, step: 1,
      description: '确认趋势反转所需的 bar 数量。', suggested: '3' },
    { key: 'co.exec.fc_adx_min', label: '反转最低 ADX', type: 'number', defaultValue: 30, min: 15, max: 50, step: 1,
      description: '触发反转平仓的最低 ADX。', suggested: '30' },
    { key: 'co.exec.position_check_min', label: '持仓检查间隔(分钟)', type: 'number', defaultValue: 15, min: 5, max: 60, step: 5,
      description: '定期检查持仓并决定是否强平的周期(分钟)。', suggested: '15' },
  ],

  // ── G8 重构方案 v2（精准买点 / 微观态 / 单一门槛 θ）──
  // [2026-08-05] 独立分组：v2 路径全部参数（去除 apply_v2 硬编码），与 v1 参数解耦。
  v2: [
    { key: 'co.v2_enabled', label: 'v2 路径总开关', type: 'switch', defaultValue: false,
      description: '灰度开关。开启后 apply_v2 覆盖 v1 决策；关闭则零改动。建议先 shadow 观察日志再切。', suggested: 'false' },
    { key: 'co.v2.pullback_block_enabled', label: '回踩保护·总开关', type: 'switch', defaultValue: true,
      description: '[2026-08-05] 行情回调(微观态 TREND_PULLBACK=价格逆趋势回撤)时停止下顺势趋势单，避免回踩未结束/演变为反转被扫损。默认开启。逆势单本就在 H1 禁区作废，不受影响。', suggested: 'true（开）' },
    { key: 'co.v2.pullback_block_depth_atr', label: '回踩保护·深度阈值(ATR)', type: 'number', defaultValue: 0.0, min: 0, max: 5, step: 0.1,
      description: '[2026-08-05] 仅当回踩深度 ≥ 此值(ATR)才拦顺势单：0=任何回踩都拦；调大(如1.5)则仅深度回调才拦、浅回踩仍作为买点保留。', suggested: '0.0（任何回踩都拦）' },
    { key: 'co.v2.weight.align', label: '权重·方向对齐', type: 'number', defaultValue: 0.40, min: 0, max: 1, step: 0.05,
      description: '方向对齐度权重（信号方向 vs 微观态方向：对齐1/反向0/无0.5）。', suggested: '0.40' },
    { key: 'co.v2.weight.structure', label: '权重·微观结构', type: 'number', defaultValue: 0.40, min: 0, max: 1, step: 0.05,
      description: '微观结构质量权重。', suggested: '0.40' },
    { key: 'co.v2.weight.rr', label: '权重·风险回报', type: 'number', defaultValue: 0.20, min: 0, max: 1, step: 0.05,
      description: '风险回报权重。', suggested: '0.20' },
    { key: 'co.v2.min_rr', label: 'R:R 硬门槛', type: 'number', defaultValue: 1.2, min: 1, max: 3, step: 0.1,
      description: 'R:R 低于此值时 entry_quality ×0.6（折扣）。', suggested: '1.2' },
    { key: 'co.v2.pullback_atr_min', label: '回踩 ATR 下限', type: 'number', defaultValue: 0.5, min: 0.1, max: 3, step: 0.1,
      description: 'TREND_PULLBACK 回踩深度 ATR 下限。', suggested: '0.5' },
    { key: 'co.v2.pullback_atr_max', label: '回踩 ATR 上限', type: 'number', defaultValue: 1.5, min: 0.5, max: 5, step: 0.1,
      description: 'TREND_PULLBACK 回踩深度 ATR 上限（zone 分量 = 1−|ΔATR−1.0|/0.5）。', suggested: '1.5' },
    { key: 'co.v2.theta.TREND_PULLBACK', label: 'θ·趋势回踩', type: 'number', defaultValue: 0.30, min: 0, max: 0.85, step: 0.01,
      description: '核心买点门槛（最低，鼓励）。运行受 vol_factor 缩放约 0.255~0.345。', suggested: '0.30' },
    { key: 'co.v2.theta.TREND_ACCEL', label: 'θ·趋势加速', type: 'number', defaultValue: 0.45, min: 0, max: 0.85, step: 0.01,
      description: '加速态门槛（不追）。', suggested: '0.45' },
    { key: 'co.v2.theta.TREND_EXHAUST', label: 'θ·趋势衰竭', type: 'number', defaultValue: 0.55, min: 0, max: 0.85, step: 0.01,
      description: '衰竭态门槛（最谨慎）。', suggested: '0.55' },
    { key: 'co.v2.theta.RANGE', label: 'θ·震荡边界', type: 'number', defaultValue: 0.45, min: 0, max: 0.85, step: 0.01,
      description: '震荡边界态门槛。', suggested: '0.45' },
    { key: 'co.v2.theta.REVERSAL', label: 'θ·反转起点', type: 'number', defaultValue: 0.50, min: 0, max: 0.85, step: 0.01,
      description: '反转起点态门槛。', suggested: '0.50' },
    { key: 'co.v2.filter.f2_discount', label: '折扣·F2收口', type: 'number', defaultValue: 0.15, min: 0, max: 0.5, step: 0.01,
      description: 'v2 下 F2 布林收口折扣量（去除硬编码）。', suggested: '0.15' },
    { key: 'co.v2.filter.f4_discount', label: '折扣·F4钝化', type: 'number', defaultValue: 0.15, min: 0, max: 0.5, step: 0.01,
      description: 'v2 下 F4 RSI 钝化折扣量（去除硬编码）。', suggested: '0.15' },
    { key: 'co.v2.exec.lot_mult', label: '执行·LOT倍率', type: 'number', defaultValue: 1.0, min: 0.1, max: 5, step: 0.1,
      description: 'v2 通过后的 LOT 倍率（去除硬编码）。', suggested: '1.0' },
    { key: 'co.v2.exec.sl_atr_mult', label: '执行·SL ATR倍数', type: 'number', defaultValue: 2.0, min: 0.5, max: 5, step: 0.1,
      description: 'v2 通过后的 SL ATR 倍数（去除硬编码）。', suggested: '2.0' },
    { key: 'co.v2.exec.rr_min', label: '执行·R:R下限', type: 'number', defaultValue: 1.2, min: 1, max: 3, step: 0.1,
      description: 'v2 通过后的 R:R 下限（去除硬编码）。', suggested: '1.2' },
  ],
};

const GROUP_SUMMARY: Record<string, string> = {
  calib: '各体制评分校准乘数',
  filter: 'F1–F6 假信号过滤器（背离/收口/数据窗口/钝化/熔断/棒质量）',
  gate: '强/弱趋势 与 突发波动 三档自适应入市门槛 + 方向分离',
  ai: 'C1–C3 批量 AI 标注 + 紧急补充调用',
  batch: '批量调度：时段/日历/回看/超时/紧急上限',
  optuna: 'TPE 自动寻参（样本内/外评估）',
  exec: 'FORCE_CLOSE 强平执行增强',
  v2: 'v2 路径：权重 / 微观态回踩 / 单一门槛θ / 折扣 / 执行倍率（去除硬编码，独立分组）',
};

const GROUP_ORDER = ['calib', 'filter', 'gate', 'ai', 'batch', 'optuna', 'exec', 'v2'];

const GROUP_TITLES: Record<string, string> = {
  calib: 'G1 校准因子 (Calib)',
  filter: 'G2 假信号过滤 (Filter)',
  gate: 'G3 自适应入市门槛 (Gate)',
  ai: 'G4 AI 标注 (AI Annotation)',
  batch: 'G5 批量调度 (Batch Scheduling)',
  optuna: 'G6 Optuna 自动调参',
  exec: 'G7 执行增强 / FORCE_CLOSE',
  v2: 'G8 重构方案 v2（精准买点）',
};

const GROUP_COLORS: Record<string, string> = {
  calib: '#22c55e',
  filter: '#3b82f6',
  gate: '#f59e0b',
  ai: '#a855f7',
  batch: '#ec4899',
  optuna: '#14b8a6',
  exec: '#ef4444',
  v2: '#7c3aed',
};

// 扁平 ConfigField[]（兼容 ConfigForm.grouped 的 buildGroups：用 type:'section' 标记分组）
const buildCoSourceFields = (): ConfigField[] => {
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

export default function CoSourceConfig() {
  const [config, setConfig] = useState<CoFilterConfig>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<{ msg: string; severity: 'success' | 'error' } | null>(null);
  const [guardOpen, setGuardOpen] = useState(false);
  // 一次性密码鉴权凭证：仅当守卫弹窗密码校验通过后才置 true，保存后立即消费
  const pwVerified = useRef(false);
  // 精确指向配置表单的命令式提交句柄，避免全局 document.querySelector('form') 歧义
  const formRef = useRef<ConfigFormHandle>(null);
  const { user } = useAuth();

  const groupedFields = useMemo(() => buildCoSourceFields(), []);

  useEffect(() => {
    (async () => {
      try {
        // [2026-07-24 修复] client.get 返回完整 AxiosResponse，response.data 是后端
        // 包裹 {code,data,message}，真实配置在 data.data。此前直接 setConfig(data)
        // 把整个包裹当配置传 initialValues，字段 co.* 全都回退默认值 → 面板不显示真实值。
        const resp = await client.get<CoFilterConfig>('/api/cosource/config');
        const body = resp.data as {
          code?: number;
          data?: CoFilterConfig;
          message?: string;
        };
        const d = body && body.data ? body.data : (resp.data as CoFilterConfig);
        setConfig(d || {});
      } catch (e) {
        console.error('加载共源信号配置失败', e);
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
    try {
      await client.put('/api/cosource/config', { updates });
      setConfig(values as CoFilterConfig);
      setToast({ msg: '共源信号配置已保存', severity: 'success' });
    } catch (e) {
      console.error('保存共源信号配置失败', e);
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

  return (
    <Box>
      <Card>
        <CardHeader
          title="共源信号配置 (Co-Source / PRD v1.1)"
          subheader="双源信号增强：双源评分融合。悬停任一参数可看「作用」与「建议值」。"
          action={
            <Button
              variant="contained"
              startIcon={<SaveIcon />}
              disabled={saving}
              onClick={() => setGuardOpen(true)}
            >
              {saving ? '保存中…' : '保存配置'}
            </Button>
          }
        />
        <Divider />
        <CardContent>
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
        </CardContent>
      </Card>

      {/* ── 保存守卫：二次确认 + 密码鉴权 ── */}
      <SaveGuardDialog
        open={guardOpen}
        title="确认保存共源信号参数"
        description="即将写入共源信号（双源）配置中心（PG↔Redis）。请确认参数无误，输入登录密码授权保存。"
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
