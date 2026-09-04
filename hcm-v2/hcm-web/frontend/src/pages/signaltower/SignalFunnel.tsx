import React, { useEffect, useState, useMemo } from 'react';
import {
  Box, Typography, Paper, Button, ButtonGroup, FormControl, Select, MenuItem,
  Table, TableHead, TableBody, TableRow, TableCell, CircularProgress, Chip,
} from '@mui/material';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import * as echarts from 'echarts/core';
import { FunnelChart, BarChart } from 'echarts/charts';
import { TooltipComponent, GridComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import client from '../../api/client';
import { classifyReason, REASON_CATEGORIES } from '../../utils/funnelReasons';

echarts.use([FunnelChart, BarChart, TooltipComponent, GridComponent, CanvasRenderer]);

interface Layer { key: string; label: string; passed: number; blocked: number; }
interface DetailRow {
  symbol: string; created_at: string | null; direction: string;
  pre_score: number | null; reason: string | null; reason_cn?: string | null;
  regime: string | null; adx: number | null;
  status: number; outcome: string; marked: boolean;
  indicators: Record<string, any> | null; rsi: number | null; macd: number | null;
  atr: number | null; h1_dir: string | null; h1_regime: string | null;
  h1_strength: number | null; h1_adx: number | null;
  comp_scores: Record<string, number[]> | null; collab: Record<string, any> | null;
  top_factor: string | null; top_factor_val: number | null;
  zone_level: number | null; zone_type: string | null; weight_scheme: string | null;
}
interface Thresholds {
  'co.gate.strong.trend': number; 'co.gate.weak.trend': number; 'co.gate.adx_strong': number;
  'co.gate.direction_min_score': number; 'co.gate.range.block': boolean;
  'scoring.min_adx_for_trade': number; 'scoring.trend_strong_adx_threshold': number;
  'scoring.trend_reverse_suppress_factor': number; 'scoring.min_score_threshold': number;
  // 【2026-08-28 死键清除】移除 'scoring.trend_min_score_threshold'（无引用死键）；
  // strong_score / weak_score 由后端 co.gate.strong/weak.trend 折算，保留仅供
  // 历史信号（co_source 时代）漏斗归因展示。
  strong_score: number; weak_score: number;
}
interface FunnelData {
  window_hours: number; symbol: string | null; symbols: string[]; candidates: number;
  traded: number; filtered_total: number; discarded_total: number;
  marked_total: number; passed_not_filled: number; no_trade: number; conversion: number; layers: Layer[];
  block_reason_breakdown: Record<string, number>; marked_breakdown: Record<string, number>;
  thresholds: Thresholds; latest_adx: number | null; latest_regime: string | null; detail: DetailRow[];
  model?: string; risk_rejected?: number;
}

// 卡点类别配色 —— 委托给 utils/funnelReasons（按后端 map_funnel_reason 的中文
// 卡点名归到 8+1 个语义类别：触发成单/策略放弃/极值防护/动量逆势/评分类/
// 结构/冷却/风控硬拦/数据引擎）。明细表与分布图统一用 REASON_CATEGORIES 着色。

// 8+1 类别固定展示顺序（与 funnelReasons.REASON_CATEGORIES 顺序一致），用于把
// 闸门链/拦截分布图按语义类别重排（而非原 grade/extreme/direction/cooldown 粗分桶）。
const CATEGORY_ORDER = ['trigger', 'strategic', 'extreme', 'momentum', 'score', 'structure', 'cooldown', 'risk', 'engine', 'neutral'] as const;

// 分量因子中文名 + 展示顺序（按策略体系）
const FACTOR_LABELS: Record<string, string> = {
  adx: 'ADX', rsi: 'RSI', boll: '布林带', macd: 'MACD', stoch: '随机指标',
  boll_vol: '布林带宽', bar_momentum: '动量', ma_alignment: '均线排列',
};
const FACTOR_ORDER = ['ma_alignment', 'rsi', 'boll', 'macd', 'stoch', 'bar_momentum', 'boll_vol', 'adx'];

const WINDOWS = [6, 24, 72];

/** 按 ADX 判定该信号所属行情带所需的共源门槛（强/弱带） */
function bandNeed(adx: number | null, t: Thresholds): { label: string; need: number; color: string } {
  if (adx == null) return { label: '—', need: t.weak_score, color: '#94a3b8' };
  if (adx >= (t['co.gate.adx_strong'] ?? 20)) return { label: '强趋势带', need: t.strong_score, color: '#ef4444' };
  return { label: '弱趋势 / 震荡带', need: t.weak_score, color: '#eab308' };
}

/** 渲染分量正向得分横条图（直观展示"基于哪些指标触发"） */
function FactorBars({ comp }: { comp: Record<string, number[]> | null }) {
  if (!comp) return <Typography variant="caption" className="text-slate-500">无分量评分数据</Typography>;
  const names = FACTOR_ORDER.filter((k) => k in comp);
  const vals = names.map((k) => {
    const v = comp[k];
    const pos = Array.isArray(v) && v.length >= 1 ? v[0] : (typeof v === 'number' ? v : 0);
    return Math.max(0, Number(pos) || 0);
  });
  if (names.length === 0) return <Typography variant="caption" className="text-slate-500">无分量评分数据</Typography>;
  const max = Math.max(...vals, 0.0001) * 1.25;
  const option = {
    grid: { left: 78, right: 36, top: 8, bottom: 8 },
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    xAxis: { type: 'value', max: Number(max.toFixed(4)), axisLabel: { color: '#94a3b8' }, splitLine: { lineStyle: { color: '#334155' } } },
    yAxis: { type: 'category', data: names.map((n) => FACTOR_LABELS[n] || n), axisLabel: { color: '#cbd5e1' } },
    series: [{
      type: 'bar', data: vals.map((v) => Number(v.toFixed(4))),
      itemStyle: { color: '#3b82f6', borderRadius: [0, 3, 3, 0] },
      label: { show: true, position: 'right', color: '#cbd5e1', fontSize: 11 },
      barWidth: '55%',
    }],
  };
  return <ReactEChartsCore echarts={echarts} option={option} style={{ height: 28 * names.length + 24 }} notMerge />;
}

/** 指标快照小卡片 */
function IndicatorChip({ label, value, accent }: { label: string; value: string | number | null; accent?: string }) {
  return (
    <Box className="p-2 rounded bg-gray-900 border border-gray-700 min-w-[92px]">
      <Typography variant="caption" className="text-slate-400 block">{label}</Typography>
      <Typography variant="body2" style={{ color: accent || '#e2e8f0' }}>{value == null ? '—' : value}</Typography>
    </Box>
  );
}

/** 单行触发画像展开详情 */
function TriggerProfile({ d, t }: { d: DetailRow; t: Thresholds }) {
  const band = bandNeed(d.adx, t);
  const passed = d.pre_score != null && d.pre_score >= band.need;
  const withTrend = d.h1_dir && (d.direction === d.h1_dir);
  const zone = d.zone_level != null && d.zone_type
    ? `${d.zone_type} @ ${d.zone_level.toFixed(2)}` : '—';
  const collab = d.collab || {};
  return (
    <Box className="p-3 rounded bg-gray-900 border border-gray-700">
      <Box className="flex items-center gap-2 flex-wrap mb-2">
        <Chip size="small" label={`权重方案 ${d.weight_scheme || '—'}`} variant="outlined" sx={{ color: '#a855f7', borderColor: '#a855f7' }} />
        <Chip size="small" label={withTrend ? `顺势 (H1 ${d.h1_dir})` : `逆势/中性 (H1 ${d.h1_dir || '—'})`}
          sx={{ color: withTrend ? '#22c55e' : '#f97316', borderColor: withTrend ? '#22c55e' : '#f97316' }} variant="outlined" />
        <Chip size="small" label={`共源门槛 ${band.label} ≥${band.need}`} variant="outlined" sx={{ color: band.color, borderColor: band.color }} />
        <Chip size="small" label={passed ? '已过门槛 ✓' : '未过门槛 ✗'} sx={{ color: passed ? '#22c55e' : '#ef4444', borderColor: passed ? '#22c55e' : '#ef4444' }} variant="outlined" />
        <Chip size="small" label={`结构位 ${zone}`} variant="outlined" sx={{ color: '#38bdf8', borderColor: '#38bdf8' }} />
      </Box>
      <Box className="flex gap-2 flex-wrap mb-3">
        <IndicatorChip label="ADX(14)" value={d.adx != null ? d.adx.toFixed(1) : null} />
        <IndicatorChip label="RSI(14)" value={d.rsi != null ? d.rsi.toFixed(1) : null} />
        <IndicatorChip label="MACD" value={d.macd != null ? d.macd.toFixed(2) : null} />
        <IndicatorChip label="ATR(14)" value={d.atr != null ? d.atr.toFixed(2) : null} />
        <IndicatorChip label="H1 方向" value={d.h1_dir || '—'} accent={withTrend ? '#22c55e' : '#f97316'} />
        <IndicatorChip label="H1 强度" value={d.h1_strength != null ? d.h1_strength.toFixed(2) : null} />
        <IndicatorChip label="H1 ADX" value={d.h1_adx != null ? d.h1_adx.toFixed(1) : null} />
        <IndicatorChip label="体制" value={d.regime || '—'} />
        <IndicatorChip label="SL 倍数" value={collab.ai_sl_mult != null ? Number(collab.ai_sl_mult).toFixed(1) : '—'} />
        <IndicatorChip label="TP 倍数" value={collab.ai_tp_mult != null ? Number(collab.ai_tp_mult).toFixed(1) : '—'} />
        <IndicatorChip label="建议手数比" value={collab.suggested_lot_ratio != null ? Number(collab.suggested_lot_ratio).toFixed(2) : '—'} />
      </Box>
      <Typography variant="caption" className="text-slate-400">触发因子 · 各分量正相关得分（越高越驱动该方向）</Typography>
      <FactorBars comp={d.comp_scores} />
      {d.reason ? (
        <Typography variant="caption" className="text-slate-500 block mt-2">拦截/标记原因：{d.reason}</Typography>
      ) : null}
    </Box>
  );
}

const SignalFunnel: React.FC = () => {
  const [hours, setHours] = useState<number>(24);
  const [symbol, setSymbol] = useState<string>('');
  const [data, setData] = useState<FunnelData | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string>('');
  const [expanded, setExpanded] = useState<number>(-1);

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const qs = `/api/v1/signal-tower/funnel?hours=${hours}${symbol ? `&symbol=${encodeURIComponent(symbol)}` : ''}`;
      const { data: resp } = await client.get(qs);
      // 后端返回包裹 {code, data, message}。code===0 且 data 存在才是真实漏斗数据；
      // 后端异常(code≠0 / data=null)时【绝不能】把整个包裹当 FunnelData 存入，否则
      // 渲染 data.layers.map 会在 undefined 上抛错 → 整页黑屏。异常时仅 setError 展示提示。
      if (resp && resp.code === 0 && resp.data) {
        setData(resp.data as FunnelData);
        setError('');
        setExpanded(-1);
      } else {
        setError((resp && resp.message) || '无法加载信号漏斗数据');
      }
    } catch (e: any) {
      setError(e?.message || '加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [hours, symbol]);

  const funnelOption = useMemo(() => {
    if (!data) return {};
    const max = data.candidates || 1;
    return {
      tooltip: { trigger: 'item', formatter: (p: any) => `${p.name}<br/>通过: ${p.value}` },
      series: [{
        type: 'funnel', top: 10, bottom: 10, left: '8%', width: '84%',
        min: 0, max, sort: 'none', gap: 2,
        label: { show: true, position: 'inside', color: '#fff', fontSize: 12, formatter: (p: any) => `${p.name}: ${p.value}` },
        labelLine: { show: false },
        itemStyle: { borderColor: '#0f172a', borderWidth: 1 },
        color: ['#3b82f6', '#22c55e', '#eab308', '#f97316', '#ef4444', '#a855f7'],
        data: data.layers.map((l) => ({ name: l.label, value: l.passed })),
      }],
    };
  }, [data]);

  const currentBand = useMemo(() => {
    if (!data || data.latest_adx == null) return null;
    return bandNeed(data.latest_adx, data.thresholds);
  }, [data]);

  // 聚焦"最新下单信号"：detail 按时间倒序，取第一条 BUY/SELL 决策（无论最终是否成交）
  const focusSignal = useMemo(() => {
    if (!data) return null;
    return data.detail.find((d) => d.direction === 'BUY' || d.direction === 'SELL') || null;
  }, [data]);

  return (
    <Box>
      <Box className="flex items-center justify-between flex-wrap gap-3 mb-4">
        <Box className="flex items-center gap-2">
          <Typography variant="h5" className="text-slate-100 font-semibold">信号漏斗 · 为何不下单 / 凭何下单</Typography>
          {data?.model === 'hexp' && (
            <Chip size="small" label="主路径: HEXP 和乘幂" sx={{ color: '#a855f7', borderColor: '#a855f7' }} variant="outlined" />
          )}
          {/* 【2026-08-28 co_source 清除】原「主路径: co_source 共源」Chip 移除；
              后端 _detect_active_model 已不再返回 'co_source'，该分支恒不触发。 */}
        </Box>
        <Box className="flex items-center gap-3">
          <FormControl size="small" sx={{ minWidth: 140 }}>
            <Select value={symbol} onChange={(e) => setSymbol(e.target.value)} displayEmpty>
              <MenuItem value="">全部品种</MenuItem>
              {(data?.symbols || []).map((s) => (<MenuItem key={s} value={s}>{s}</MenuItem>))}
            </Select>
          </FormControl>
          <ButtonGroup size="small" variant="outlined">
            {WINDOWS.map((w) => (
              <Button key={w} variant={hours === w ? 'contained' : 'outlined'} onClick={() => setHours(w)}>
                {w}h
              </Button>
            ))}
          </ButtonGroup>
        </Box>
      </Box>

      {loading && <Box className="flex justify-center py-10"><CircularProgress /></Box>}
      {error && <Typography color="error" className="mb-3">{error}</Typography>}

      {data && !loading && (
        <Box className="flex flex-col gap-4">
          {/* 概览 */}
          <Box className="flex gap-3 flex-wrap">
            <Paper className="flex-1 min-w-[150px] p-3 bg-gray-800">
              <Typography variant="caption" className="text-slate-400">候选信号</Typography>
              <Typography variant="h6" className="text-blue-400">{data.candidates}</Typography>
            </Paper>
            <Paper className="flex-1 min-w-[150px] p-3 bg-gray-800">
              <Typography variant="caption" className="text-slate-400">实际成交 (MT5)</Typography>
              <Typography variant="h6" className="text-green-400">{data.traded}</Typography>
            </Paper>
            <Paper className="flex-1 min-w-[150px] p-3 bg-gray-800">
              <Typography variant="caption" className="text-slate-400">成交转化率</Typography>
              <Typography variant="h6" className="text-purple-400">{(data.conversion * 100).toFixed(1)}%</Typography>
            </Paper>
            {data.model === 'hexp' ? (
              <Paper className="flex-1 min-w-[150px] p-3 bg-gray-800">
                <Typography variant="caption" className="text-slate-400">HEXP 分级下限</Typography>
                <Typography variant="h6" className="text-purple-400">{String((data.thresholds as any)['hexp.min_grade'] ?? 'C')}</Typography>
              </Paper>
            ) : (
              <Paper className="flex-1 min-w-[150px] p-3 bg-gray-800">
                <Typography variant="caption" className="text-slate-400">当前行情带</Typography>
                <Typography variant="h6" style={{ color: currentBand?.color || '#94a3b8' }}>
                  {currentBand ? `${currentBand.label} 需≥${currentBand.need}` : '—'}
                </Typography>
              </Paper>
            )}
          </Box>

          {/* 被标记 vs 真丢弃 区分（按主路径引擎切换语义） */}
          <Box className="flex gap-3 flex-wrap">
            {data.model === 'hexp' ? (
              <>
                <Paper className="flex-1 min-w-[150px] p-3 bg-gray-800 border-l-4 border-red-500">
                  <Typography variant="caption" className="text-slate-400">HEXP 拦截 (未发风控)</Typography>
                  <Typography variant="h6" className="text-red-400">{data.discarded_total}</Typography>
                  <Typography variant="caption" className="text-slate-500">HEXP 闸门拒发交易(status=0, NO_TRADE)</Typography>
                </Paper>
                <Paper className="flex-1 min-w-[150px] p-3 bg-gray-800 border-l-4 border-amber-400">
                  <Typography variant="caption" className="text-slate-400">风控拒绝</Typography>
                  <Typography variant="h6" className="text-amber-400">{data.risk_rejected ?? 0}</Typography>
                  <Typography variant="caption" className="text-slate-500">已发风控 status=2 真丢弃</Typography>
                </Paper>
                <Paper className="flex-1 min-w-[150px] p-3 bg-gray-800 border-l-4 border-slate-500">
                  <Typography variant="caption" className="text-slate-400">过闸门未成交</Typography>
                  <Typography variant="h6" className="text-slate-300">{data.passed_not_filled}</Typography>
                  <Typography variant="caption" className="text-slate-500">在途/过风控未成交(status=0/1)</Typography>
                </Paper>
                <Paper className="flex-1 min-w-[150px] p-3 bg-gray-800 border-l-4 border-sky-500">
                  <Typography variant="caption" className="text-slate-400">引擎未运行/无数据</Typography>
                  <Typography variant="h6" className="text-sky-400">{data.no_trade}</Typography>
                  <Typography variant="caption" className="text-slate-500">非 hexp 闸门的 filtered(其它)</Typography>
                </Paper>
              </>
            ) : (
              <>
                <Paper className="flex-1 min-w-[150px] p-3 bg-gray-800 border-l-4 border-red-500">
                  <Typography variant="caption" className="text-slate-400">真丢弃 (风控拒绝)</Typography>
                  <Typography variant="h6" className="text-red-400">{data.discarded_total}</Typography>
                  <Typography variant="caption" className="text-slate-500">signal_status=2，确实没下单</Typography>
                </Paper>
                <Paper className="flex-1 min-w-[150px] p-3 bg-gray-800 border-l-4 border-amber-400">
                  <Typography variant="caption" className="text-slate-400">被标记仍成交</Typography>
                  <Typography variant="h6" className="text-amber-400">{data.marked_total}</Typography>
                  <Typography variant="caption" className="text-slate-500">如 F5 软扣分，但照常开仓</Typography>
                </Paper>
                <Paper className="flex-1 min-w-[150px] p-3 bg-gray-800 border-l-4 border-slate-500">
                  <Typography variant="caption" className="text-slate-400">过闸门未成交</Typography>
                  <Typography variant="h6" className="text-slate-300">{data.passed_not_filled}</Typography>
                  <Typography variant="caption" className="text-slate-500">BUY/SELL 过风控但未成交(真漏单)</Typography>
                </Paper>
                <Paper className="flex-1 min-w-[150px] p-3 bg-gray-800 border-l-4 border-sky-500">
                  <Typography variant="caption" className="text-slate-400">策略放弃 (NO_TRADE)</Typography>
                  <Typography variant="h6" className="text-sky-400">{data.no_trade}</Typography>
                  <Typography variant="caption" className="text-slate-500">主动不交易，非漏单</Typography>
                </Paper>
              </>
            )}
          </Box>

          {/* 最新下单信号触发画像 */}
          {focusSignal && (
            <Paper className="p-3 bg-gray-800">
              <Typography variant="subtitle2" className="text-slate-300 mb-1">
                最新下单信号触发画像 · {focusSignal.direction} @ {focusSignal.created_at ? focusSignal.created_at.replace('T', ' ').slice(0, 19) : '—'}
              </Typography>
              <Typography variant="caption" className="text-slate-500">直观展示这笔 {focusSignal.direction} 决策由哪些指标 / 参数驱动触发（含 H1 顺势判定、共源门槛对比、结构位）。</Typography>
              <Box className="mt-2">
                <TriggerProfile d={focusSignal} t={data.thresholds} />
              </Box>
            </Paper>
          )}

          {/* 漏斗图（通过量）+ 拦截原因分布（8+1 语义类别重排） */}
          <Box className="flex flex-col lg:flex-row gap-4">
            <Paper className="flex-1 p-3 bg-gray-800 min-h-[360px]">
              <Typography variant="subtitle2" className="text-slate-300 mb-1">逐层漏斗（通过量）</Typography>
              <ReactEChartsCore echarts={echarts} option={funnelOption} style={{ height: 340 }} notMerge />
            </Paper>
            <Paper className="flex-1 p-3 bg-gray-800">
              <Typography variant="subtitle2" className="text-slate-300 mb-1">拦截原因分布（8+1 语义类别）</Typography>
              <Typography variant="caption" className="text-slate-500">按「触发成单 / 策略放弃 / 极值防护 / 动量逆势 / 评分类 / 结构 / 冷却 / 风控硬拦 / 数据引擎」固定顺序聚合拦截原因（替代原 grade/extreme/direction/cooldown 粗分桶）。</Typography>
              <Box className="flex flex-col gap-2 mt-2">
                {CATEGORY_ORDER.filter((k) => (data.block_reason_breakdown[k] || 0) > 0).map((k) => {
                  const cat = REASON_CATEGORIES[k] || REASON_CATEGORIES.neutral;
                  return (
                    <Box key={k} className="p-2 rounded bg-gray-900 border-l-4" style={{ borderColor: cat.color }}>
                      <Box className="flex justify-between items-center">
                        <Typography variant="body2" className="text-slate-200">{cat.label}</Typography>
                        <Typography variant="body2" style={{ color: cat.color }}>{data.block_reason_breakdown[k]}</Typography>
                      </Box>
                    </Box>
                  );
                })}
                {CATEGORY_ORDER.filter((k) => (data.block_reason_breakdown[k] || 0) > 0).length === 0 && (
                  <Typography variant="caption" className="text-slate-500">近 {data.window_hours}h 无拦截记录</Typography>
                )}
              </Box>
              {CATEGORY_ORDER.filter((k) => (data.marked_breakdown[k] || 0) > 0).length > 0 && (
                <Box className="mt-3">
                  <Typography variant="caption" className="text-slate-400">被标记仍成交</Typography>
                  <Box className="flex flex-col gap-1 mt-1">
                    {CATEGORY_ORDER.filter((k) => (data.marked_breakdown[k] || 0) > 0).map((k) => {
                      const cat = REASON_CATEGORIES[k] || REASON_CATEGORIES.neutral;
                      return (
                        <Box key={k} className="flex justify-between items-center text-sm">
                          <Typography variant="caption" className="text-slate-300">{cat.label}</Typography>
                          <Typography variant="caption" style={{ color: cat.color }}>{data.marked_breakdown[k]}</Typography>
                        </Box>
                      );
                    })}
                  </Box>
                </Box>
              )}
            </Paper>
          </Box>

          {/* 最近明细 */}
          <Paper className="p-3 bg-gray-800">
            <Typography variant="subtitle2" className="text-slate-300 mb-2">最近信号明细（被标记 / 真丢弃 / 成交）· 点击行展开触发画像</Typography>
            <Box className="overflow-x-auto">
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell className="text-slate-400">时间</TableCell>
                    <TableCell className="text-slate-400">品种</TableCell>
                    <TableCell className="text-slate-400">方向</TableCell>
                    <TableCell className="text-slate-400">pre_score</TableCell>
                    <TableCell className="text-slate-400">ADX</TableCell>
                    <TableCell className="text-slate-400">RSI</TableCell>
                    <TableCell className="text-slate-400">H1方向</TableCell>
                    <TableCell className="text-slate-400">最强因子</TableCell>
                    <TableCell className="text-slate-400">处置</TableCell>
                    <TableCell className="text-slate-400">标记</TableCell>
                    <TableCell className="text-slate-400">原因</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {data.detail.map((d, i) => {
                    const oc = d.outcome === '成交' ? '#22c55e'
                      : d.outcome === '真丢弃' ? '#ef4444'
                      : d.outcome === '过闸门未成交' ? '#94a3b8'
                      : d.outcome === '策略放弃' ? '#38bdf8' : '#64748b';
                      const cat = classifyReason(d.reason_cn || d.reason || '');
                      const isOpen = expanded === i;
                    return (
                      <React.Fragment key={i}>
                        <TableRow
                          hover
                          onClick={() => setExpanded(isOpen ? -1 : i)}
                          style={{ cursor: 'pointer' }}
                        >
                          <TableCell className="text-slate-300 whitespace-nowrap">{d.created_at ? d.created_at.replace('T', ' ').slice(0, 19) : '-'}</TableCell>
                          <TableCell className="text-slate-300">{d.symbol}</TableCell>
                          <TableCell className="text-slate-300">{d.direction}</TableCell>
                          <TableCell className="text-slate-300">{d.pre_score != null ? d.pre_score.toFixed(3) : '-'}</TableCell>
                          <TableCell className="text-slate-300">{d.adx != null ? d.adx.toFixed(1) : '-'}</TableCell>
                          <TableCell className="text-slate-300">{d.rsi != null ? d.rsi.toFixed(1) : '-'}</TableCell>
                          <TableCell className="text-slate-300">{d.h1_dir || '-'}</TableCell>
                          <TableCell className="text-slate-300 whitespace-nowrap">
                            {d.top_factor ? `${FACTOR_LABELS[d.top_factor] || d.top_factor}${d.top_factor_val != null ? ` (${d.top_factor_val.toFixed(2)})` : ''}` : '-'}
                          </TableCell>
                          <TableCell>
                            <Chip size="small" label={d.outcome} sx={{ color: oc, borderColor: oc }} variant="outlined" />
                          </TableCell>
                          <TableCell>{d.marked ? <Chip size="small" label="已标记" color="warning" variant="outlined" /> : '-'}</TableCell>
                          <TableCell className="max-w-[320px]">
                            {d.reason_cn || d.reason ? (
                              <Box className="flex items-center gap-2" title={d.reason || ''}>
                                {/* 类别色条：一眼区分该信号归属的语义类别 */}
                                <Box component="span" sx={{ width: 4, height: 18, borderRadius: 2, backgroundColor: cat.color, flexShrink: 0 }} />
                                <Box className="flex flex-col min-w-0">
                                  <Chip
                                    size="small"
                                    variant="outlined"
                                    label={cat.label}
                                    sx={{ color: cat.color, borderColor: cat.color, alignSelf: 'flex-start', mb: 0.25 }}
                                  />
                                  <Typography
                                    variant="caption"
                                    className="text-slate-400"
                                    sx={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 270, display: 'block' }}
                                  >
                                    {d.reason_cn || d.reason}
                                  </Typography>
                                </Box>
                              </Box>
                            ) : (
                              <span className="text-slate-500">-</span>
                            )}
                          </TableCell>
                        </TableRow>
                        {isOpen && (
                          <TableRow>
                            <TableCell colSpan={11} style={{ padding: 0, background: '#0f172a' }}>
                              <TriggerProfile d={d} t={data.thresholds} />
                            </TableCell>
                          </TableRow>
                        )}
                      </React.Fragment>
                    );
                  })}
                </TableBody>
              </Table>
            </Box>
          </Paper>
        </Box>
      )}
    </Box>
  );
};

export default SignalFunnel;
