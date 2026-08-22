import React, { useEffect, useState } from 'react';
import {
  Box, Tabs, Tab, Paper, Typography, Table, TableBody, TableCell,
  TableContainer, TableHead, TableRow, Alert, CircularProgress,
  TextField, Button, Stack, Chip, Select, MenuItem,
} from '@mui/material';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import * as echarts from 'echarts/core';
import { BarChart, LineChart, PieChart } from 'echarts/charts';
import { GridComponent, TooltipComponent, LegendComponent, MarkLineComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import KpiCard from '../../components/KpiCard';
import client from '../../api/client';
import { ENDPOINTS } from '../../api/endpoints';

echarts.use([BarChart, LineChart, PieChart, GridComponent, TooltipComponent, LegendComponent, MarkLineComponent, CanvasRenderer]);

/** AI 报表模块 — 4 个报表（系统健康 / 信号分层 / 绩效对比 / 快照明细）。只读。 */

interface HealthData {
  window_hours: number;
  events: Record<string, number>;
  lm_inference_count: number;
  last_inference_ts: string | null;
  ds: { ok: number; fail: number; success_rate: number | null; cache_hit: number };
}
interface LayerData {
  window_days: number;
  candidates: { regime: string; cnt: number }[];
  candidates_total: number;
  gate_actions: { regime: string; action: string; cnt: number }[];
  filter_rate: number | null;
  note: string;
}
interface PerfData {
  window_days: number;
  live: { total: number; closed: number; sum_profit: number; win_cnt: number; loss_cnt: number } | null;
  live_max_drawdown: number;
  baseline_shadow: { total: number; win_cnt: number; loss_cnt: number; avg_pnl_r: number } | null;
  note: string;
}
interface SnapRow {
  log_id: number; symbol: string; ai_score: number | null; total_score: number | null;
  ext_factor_score: number | null; sl_coeff: number | null; continuity: number | null;
  mode: string; model_version: string | null; created_at: string;
}
interface SnapData { rows: SnapRow[]; total: number; limit: number; offset: number; }

const REGIME_COLOR: Record<string, string> = { TREND: '#22c55e', NEUTRAL: '#3b82f6', RANGE: '#f59e0b', UNKNOWN: '#64748b' };
const ACTION_COLOR: Record<string, string> = { VETO: '#ef4444', DOWNGRADE: '#f59e0b', HOLD: '#64748b', UPGRADE: '#22c55e' };

function fmt(v: number | null | undefined, digits = 2): string {
  return v === null || v === undefined ? '—' : Number(v).toFixed(digits);
}

/* ── ① 系统健康监控 ─────────────────────────────────────────────── */

/* 实时 AI 评分趋势（直观看涨跌 + veto/coupling 门槛）。
 * 数据源：GET /api/v1/hexp/ai/{symbol} → sidecar 实时快照（每 5s 刷新）。
 * 每 5s 轮询，保留最近 N 个点绘制折线，含 veto_floor(30) / coupling(50) 参考线。 */
function AiScoreTrend() {
  const [points, setPoints] = useState<{ t: string; ai: number | null; total: number | null }[]>([]);
  const [symbol, setSymbol] = useState('XAUUSD');
  const [online, setOnline] = useState<boolean | null>(null);
  const MAX_POINTS = 120;

  useEffect(() => {
    const timer = setInterval(() => {
      client.get(`${ENDPOINTS.hexp.ai}/${symbol}`)
        .then((r) => {
          const d = r.data?.data ?? null;
          setOnline(!!d);
          if (!d) return;
          const ai = typeof d.ai_score === 'number' ? d.ai_score : null;
          const total = typeof d.total_score === 'number' ? d.total_score : null;
          const now = new Date();
          setPoints((prev) => {
            const next = [...prev, {
              t: now.toLocaleTimeString('zh-CN', { hour12: false }),
              ai,
              total,
            }];
            return next.length > MAX_POINTS ? next.slice(next.length - MAX_POINTS) : next;
          });
        })
        .catch(() => setOnline(false));
    }, 5000);
    return () => clearInterval(timer);
  }, [symbol]);

  const times = points.map((p) => p.t);
  const aiLine = points.map((p) => p.ai);
  const totalLine = points.map((p) => p.total);
  const option = {
    tooltip: { trigger: 'axis' },
    legend: { data: ['ai_score', 'total_score'], textStyle: { color: '#94a3b8' } },
    grid: { left: 50, right: 20, top: 50, bottom: 50 },
    xAxis: {
      type: 'category', data: times,
      axisLabel: { color: '#94a3b8', fontSize: 10 },
      axisLine: { lineStyle: { color: '#334155' } },
    },
    yAxis: {
      type: 'value', min: 0, max: 100,
      axisLabel: { color: '#94a3b8' },
      splitLine: { lineStyle: { color: '#1e293b' } },
    },
    series: [
      {
        name: 'ai_score', type: 'line', data: aiLine, smooth: true, showSymbol: false,
        itemStyle: { color: '#22c55e' }, areaStyle: { opacity: 0.15 },
        markLine: {
          silent: true, symbol: 'none',
          data: [
            { yAxis: 30, name: 'veto_floor', lineStyle: { color: '#ef4444', type: 'dashed' }, label: { color: '#ef4444', formatter: 'veto 30' } },
            { yAxis: 50, name: 'coupling_pass', lineStyle: { color: '#eab308', type: 'dashed' }, label: { color: '#eab308', formatter: 'pass 50' } },
          ],
        },
      },
      {
        name: 'total_score', type: 'line', data: totalLine, smooth: true, showSymbol: false,
        itemStyle: { color: '#3b82f6' },
      },
    ],
  };
  const latest = points.length > 0 ? points[points.length - 1] : null;
  const vetoHit = latest && latest.ai !== null && latest.ai < 30;
  return (
    <Box sx={{ mt: 3 }}>
      <Stack direction="row" spacing={2} alignItems="center" sx={{ mb: 1 }}>
        <Typography variant="subtitle2" sx={{ color: '#94a3b8' }}>实时 AI 评分趋势</Typography>
        <Select
          size="small" value={symbol}
          onChange={(e) => { setSymbol(e.target.value as string); setPoints([]); }}
          sx={{ minWidth: 140, color: '#e2e8f0', '.MuiOutlinedInput-notchedOutline': { borderColor: '#334155' } }}
        >
          {['XAUUSD', 'XAGUSD', 'EURUSD', 'GBPUSD'].map((s) => <MenuItem key={s} value={s}>{s}</MenuItem>)}
        </Select>
        <Chip
          size="small"
          label={online === null ? '探测中…' : online ? 'sidecar 在线' : 'sidecar 离线'}
          sx={{
            backgroundColor: online === null ? '#33415522' : online ? '#22c55e22' : '#ef444422',
            color: online === null ? '#94a3b8' : online ? '#34d399' : '#f87171',
          }}
        />
        {vetoHit && <Chip size="small" label="当前 ai_score<30 → 触发 VETO" sx={{ backgroundColor: '#ef444422', color: '#f87171' }} />}
      </Stack>
      <Box sx={{ height: 260 }}>
        {points.length > 1
          ? <ReactEChartsCore echarts={echarts} option={option} style={{ height: 260 }} notMerge />
          : <Typography sx={{ color: '#64748b', py: 6, textAlign: 'center' }}>采集 AI 评分中…（每 5s 一个点）</Typography>}
      </Box>
    </Box>
  );
}

function HealthReport() {
  const [d, setD] = useState<HealthData | null>(null);
  const [err, setErr] = useState('');
  useEffect(() => {
    client.get(ENDPOINTS.ai.report.health).then((r) => setD(r.data?.data ?? null)).catch((e) => setErr(String(e)));
  }, []);
  if (err) return <Alert severity="error">{err}</Alert>;
  if (!d) return <Box sx={{ p: 4, textAlign: 'center' }}><CircularProgress /></Box>;
  const rate = d.ds.success_rate;
  const degrade = (d.events?.['degrade'] ?? 0) + (d.events?.['ds_fail'] ?? 0) + (d.events?.['price_offset_fuse'] ?? 0);
  const warn = rate !== null && rate < 0.9;
  return (
    <Box>
      {warn && <Alert severity="warning" sx={{ mb: 2 }}>DeepSeek 调用成功率 {fmt(rate! * 100, 1)}% 低于 90% 阈值</Alert>}
      {/* 实时 AI 评分趋势（直观） */}
      <AiScoreTrend />
      <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap>
        <KpiCard title="LM 推理次数" value={d.lm_inference_count} color="#3b82f6" subtitle={`最近 ${d.window_hours}h`} />
        <KpiCard title="DeepSeek 成功率" value={rate === null ? '—' : `${fmt(rate * 100, 1)}%`} color={warn ? '#ef4444' : '#22c55e'} subtitle={`ok ${d.ds.ok} / fail ${d.ds.fail}`} />
        <KpiCard title="缓存命中" value={d.ds.cache_hit} color="#8b5cf6" subtitle="DeepSeek 输出缓存命中次数" />
        <KpiCard title="降级/熔断频次" value={degrade} color={degrade > 0 ? '#f59e0b' : '#22c55e'} subtitle="失败+熔断+降级事件" />
      </Stack>
      <Typography variant="subtitle2" sx={{ mt: 3, mb: 1, color: '#94a3b8' }}>事件类型分布（最近 {d.window_hours}h）</Typography>
      <TableContainer component={Paper} elevation={0} sx={{ backgroundColor: '#111118', border: '1px solid #2a2a3a' }}>
        <Table size="small">
          <TableHead><TableRow>
            <TableCell sx={{ color: '#94a3b8' }}>事件类型</TableCell>
            <TableCell sx={{ color: '#94a3b8' }} align="right">次数</TableCell>
          </TableRow></TableHead>
          <TableBody>
            {Object.entries(d.events ?? {}).map(([k, v]) => (
              <TableRow key={k}><TableCell sx={{ color: '#e2e8f0' }}>{k}</TableCell>
                <TableCell align="right" sx={{ color: '#e2e8f0' }}>{v}</TableCell></TableRow>
            ))}
            {Object.keys(d.events ?? {}).length === 0 && (
              <TableRow><TableCell colSpan={2} sx={{ color: '#64748b' }}>暂无事件（埋点数据积累中）</TableCell></TableRow>
            )}
          </TableBody>
        </Table>
      </TableContainer>
      {d.last_inference_ts && <Typography variant="caption" sx={{ color: '#64748b', display: 'block', mt: 1 }}>最近推理：{d.last_inference_ts}</Typography>}
    </Box>
  );
}

/* ── ② 信号分层统计 ─────────────────────────────────────────────── */
function LayerReport() {
  const [d, setD] = useState<LayerData | null>(null);
  const [err, setErr] = useState('');
  useEffect(() => {
    client.get(ENDPOINTS.ai.report.layer).then((r) => setD(r.data?.data ?? null)).catch((e) => setErr(String(e)));
  }, []);
  if (err) return <Alert severity="error">{err}</Alert>;
  if (!d) return <Box sx={{ p: 4, textAlign: 'center' }}><CircularProgress /></Box>;
  const regimes = d.candidates.map((c) => c.regime);
  const candVals = d.candidates.map((c) => c.cnt);
  const vetoByRegime: Record<string, number> = {};
  d.gate_actions.forEach((g) => { if (g.action === 'VETO') vetoByRegime[g.regime] = (vetoByRegime[g.regime] ?? 0) + g.cnt; });
  const barOption = {
    tooltip: { trigger: 'axis' },
    legend: { data: ['HP候选', 'AI否决'], textStyle: { color: '#94a3b8' } },
    grid: { left: 40, right: 20, top: 40, bottom: 30 },
    xAxis: { type: 'category', data: regimes, axisLabel: { color: '#94a3b8' } },
    yAxis: { type: 'value', axisLabel: { color: '#94a3b8' }, splitLine: { lineStyle: { color: '#1e293b' } } },
    series: [
      { name: 'HP候选', type: 'bar', data: candVals, itemStyle: { color: '#3b82f6' } },
      { name: 'AI否决', type: 'bar', data: regimes.map((r) => vetoByRegime[r] ?? 0), itemStyle: { color: '#ef4444' } },
    ],
  };
  return (
    <Box>
      <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap>
        <KpiCard title="HP 原生候选信号" value={d.candidates_total} color="#3b82f6" subtitle={`最近 ${d.window_days} 天`} />
        <KpiCard title="AI 过滤率" value={d.filter_rate === null ? '—' : `${fmt(d.filter_rate * 100, 1)}%`} color="#ef4444" subtitle="VETO / 候选总数" />
        <KpiCard title="TREND 候选" value={d.candidates.find((c) => c.regime === 'TREND')?.cnt ?? 0} color="#22c55e" subtitle="趋势模式" />
        <KpiCard title="RANGE 候选" value={d.candidates.find((c) => c.regime === 'RANGE')?.cnt ?? 0} color="#f59e0b" subtitle="震荡模式" />
      </Stack>
      <Box sx={{ mt: 2, height: 280 }}>
        {d.candidates.length > 0
          ? <ReactEChartsCore echarts={echarts} option={barOption} style={{ height: 280 }} />
          : <Typography sx={{ color: '#64748b' }}>暂无候选信号数据</Typography>}
      </Box>
      <Typography variant="subtitle2" sx={{ mt: 2, mb: 1, color: '#94a3b8' }}>AI 闸门决策分布（按模式 × 动作）</Typography>
      <TableContainer component={Paper} elevation={0} sx={{ backgroundColor: '#111118', border: '1px solid #2a2a3a' }}>
        <Table size="small">
          <TableHead><TableRow>
            {['regime', 'action', 'cnt'].map((h) => <TableCell key={h} sx={{ color: '#94a3b8' }}>{h}</TableCell>)}
          </TableRow></TableHead>
          <TableBody>
            {d.gate_actions.map((g, i) => (
              <TableRow key={i}>
                <TableCell sx={{ color: REGIME_COLOR[g.regime] ?? '#e2e8f0' }}>{g.regime}</TableCell>
                <TableCell><Chip size="small" label={g.action} sx={{ backgroundColor: ACTION_COLOR[g.action] ?? '#64748b', color: '#fff' }} /></TableCell>
                <TableCell sx={{ color: '#e2e8f0' }}>{g.cnt}</TableCell>
              </TableRow>
            ))}
            {d.gate_actions.length === 0 && <TableRow><TableCell colSpan={3} sx={{ color: '#64748b' }}>闸门决策尚未落库（gate 接入 scheduler 后积累）</TableCell></TableRow>}
          </TableBody>
        </Table>
      </TableContainer>
    </Box>
  );
}

/* ── ③ 交易绩效对比 ─────────────────────────────────────────────── */
function PerformanceReport() {
  const [d, setD] = useState<PerfData | null>(null);
  const [err, setErr] = useState('');
  useEffect(() => {
    client.get(ENDPOINTS.ai.report.performance).then((r) => setD(r.data?.data ?? null)).catch((e) => setErr(String(e)));
  }, []);
  if (err) return <Alert severity="error">{err}</Alert>;
  if (!d) return <Box sx={{ p: 4, textAlign: 'center' }}><CircularProgress /></Box>;
  const live = d.live;
  const base = d.baseline_shadow;
  const liveWin = live && live.closed > 0 ? (live.win_cnt / live.closed) * 100 : null;
  const baseWin = base && base.total > 0 ? (base.win_cnt / base.total) * 100 : null;
  return (
    <Box>
      <Alert severity="info" sx={{ mb: 2 }}>{d.note}</Alert>
      <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap>
        <KpiCard title="实盘累计盈亏" value={live ? `¥${fmt(live.sum_profit)}` : '—'} color={live && live.sum_profit >= 0 ? '#22c55e' : '#ef4444'} subtitle={`${live?.closed ?? 0} 笔已平仓`} />
        <KpiCard title="实盘最大回撤" value={live ? `¥${fmt(d.live_max_drawdown)}` : '—'} color="#ef4444" subtitle="累计盈亏曲线回撤" />
        <KpiCard title="实盘胜率" value={liveWin === null ? '—' : `${fmt(liveWin, 1)}%`} color="#3b82f6" subtitle={`${live?.win_cnt ?? 0} 胜 / ${live?.loss_cnt ?? 0} 负`} />
        <KpiCard title="影子基准胜率" value={baseWin === null ? '—' : `${fmt(baseWin, 1)}%`} color="#8b5cf6" subtitle={`${base?.total ?? 0} 条影子样本`} />
      </Stack>
      <Typography variant="subtitle2" sx={{ mt: 3, mb: 1, color: '#94a3b8' }}>AI 增强 vs 纯 HP 基准（胜率对比）</Typography>
      <TableContainer component={Paper} elevation={0} sx={{ backgroundColor: '#111118', border: '1px solid #2a2a3a' }}>
        <Table size="small">
          <TableHead><TableRow>
            {['组别', '样本数', '胜率', '平均盈亏 R', '累计盈亏'].map((h) => <TableCell key={h} sx={{ color: '#94a3b8' }}>{h}</TableCell>)}
          </TableRow></TableHead>
          <TableBody>
            <TableRow>
              <TableCell sx={{ color: '#3b82f6' }}>AI 增强实盘</TableCell>
              <TableCell sx={{ color: '#e2e8f0' }}>{live?.closed ?? 0}</TableCell>
              <TableCell sx={{ color: '#e2e8f0' }}>{liveWin === null ? '—' : `${fmt(liveWin, 1)}%`}</TableCell>
              <TableCell sx={{ color: '#e2e8f0' }}>—</TableCell>
              <TableCell sx={{ color: live && live.sum_profit >= 0 ? '#22c55e' : '#ef4444' }}>¥{fmt(live?.sum_profit)}</TableCell>
            </TableRow>
            <TableRow>
              <TableCell sx={{ color: '#8b5cf6' }}>纯 HP 固定止损基准</TableCell>
              <TableCell sx={{ color: '#e2e8f0' }}>{base?.total ?? 0}</TableCell>
              <TableCell sx={{ color: '#e2e8f0' }}>{baseWin === null ? '—' : `${fmt(baseWin, 1)}%`}</TableCell>
              <TableCell sx={{ color: '#e2e8f0' }}>{fmt(base?.avg_pnl_r)}</TableCell>
              <TableCell sx={{ color: '#e2e8f0' }}>—</TableCell>
            </TableRow>
          </TableBody>
        </Table>
      </TableContainer>
    </Box>
  );
}

/* ── ④ AI 快照明细 ─────────────────────────────────────────────── */
function SnapshotReport() {
  const [d, setD] = useState<SnapData | null>(null);
  const [err, setErr] = useState('');
  const [symbol, setSymbol] = useState('');
  const [minS, setMinS] = useState('');
  const [maxS, setMaxS] = useState('');
  const [offset, setOffset] = useState(0);
  const LIMIT = 100;
  const load = (off = 0) => {
    setOffset(off);
    client.get(ENDPOINTS.ai.report.snapshot, {
      params: {
        symbol: symbol || undefined,
        min_score: minS === '' ? undefined : Number(minS),
        max_score: maxS === '' ? undefined : Number(maxS),
        limit: LIMIT,
        offset: off,
      },
    }).then((r) => setD(r.data?.data ?? null)).catch((e) => setErr(String(e)));
  };
  useEffect(() => { load(0); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, []);
  if (err) return <Alert severity="error">{err}</Alert>;
  return (
    <Box>
      <Stack direction="row" spacing={2} sx={{ mb: 2 }} flexWrap="wrap" useFlexGap>
        <TextField size="small" label="品种" value={symbol} onChange={(e) => setSymbol(e.target.value)} sx={{ input: { color: '#e2e8f0' } }} />
        <TextField size="small" label="AI 分 ≥" value={minS} onChange={(e) => setMinS(e.target.value)} sx={{ input: { color: '#e2e8f0' } }} />
        <TextField size="small" label="AI 分 ≤" value={maxS} onChange={(e) => setMaxS(e.target.value)} sx={{ input: { color: '#e2e8f0' } }} />
        <Button variant="contained" onClick={() => load(0)}>检索</Button>
      </Stack>
      {!d ? <Box sx={{ p: 4, textAlign: 'center' }}><CircularProgress /></Box> : (
        <>
          <Typography variant="caption" sx={{ color: '#64748b', mb: 1, display: 'block' }}>共 {d.total} 条，第 {Math.floor(d.offset / LIMIT) + 1} 页</Typography>
          <TableContainer component={Paper} elevation={0} sx={{ backgroundColor: '#111118', border: '1px solid #2a2a3a' }}>
            <Table size="small">
              <TableHead><TableRow>
                {['时间', '品种', 'AI评分', '总分', '外部因子', 'sl_coeff', 'continuity', 'mode'].map((h) => <TableCell key={h} sx={{ color: '#94a3b8' }}>{h}</TableCell>)}
              </TableRow></TableHead>
              <TableBody>
                {d.rows.map((r) => (
                  <TableRow key={r.log_id}>
                    <TableCell sx={{ color: '#94a3b8' }}>{r.created_at ? String(r.created_at).replace('T', ' ').slice(0, 19) : '—'}</TableCell>
                    <TableCell sx={{ color: '#e2e8f0' }}>{r.symbol}</TableCell>
                    <TableCell sx={{ color: '#e2e8f0' }}>{fmt(r.ai_score)}</TableCell>
                    <TableCell sx={{ color: '#e2e8f0' }}>{fmt(r.total_score)}</TableCell>
                    <TableCell sx={{ color: '#e2e8f0' }}>{fmt(r.ext_factor_score)}</TableCell>
                    <TableCell sx={{ color: '#e2e8f0' }}>{fmt(r.sl_coeff)}</TableCell>
                    <TableCell sx={{ color: '#e2e8f0' }}>{fmt(r.continuity, 0)}</TableCell>
                    <TableCell><Chip size="small" label={r.mode} sx={{ backgroundColor: r.mode === 'coupled' ? '#22c55e' : '#64748b', color: '#fff' }} /></TableCell>
                  </TableRow>
                ))}
                {d.rows.length === 0 && <TableRow><TableCell colSpan={8} sx={{ color: '#64748b' }}>暂无推理快照（sidecar 落库后积累）</TableCell></TableRow>}
              </TableBody>
            </Table>
          </TableContainer>
          <Stack direction="row" spacing={1} sx={{ mt: 2 }}>
            <Button size="small" disabled={offset === 0} onClick={() => load(offset - LIMIT)}>上一页</Button>
            <Button size="small" disabled={offset + LIMIT >= d.total} onClick={() => load(offset + LIMIT)}>下一页</Button>
          </Stack>
        </>
      )}
    </Box>
  );
}

/* ── ⑤ 每日 KPI 聚合（方案 B 日表 + 后台每小时聚合 + AI 真实盈亏贡献）── */
interface DailyRow {
  trade_date: string;
  lm_inferences: number; ds_calls: number; ds_success: number; ds_fail: number; ds_timeout: number;
  cache_hits: number; fuse_events: number; degrade_events: number;
  hp_candidates: number; ai_passed: number; ai_vetoed: number; ai_upgraded: number;
  ai_downdgraded: number; ai_opened: number;
  total_orders: number; total_pnl: number; win_orders: number; loss_orders: number;
  ai_enhanced_orders: number; ai_enhanced_pnl: number; non_ai_pnl: number; ai_contrib_ratio: number;
  fused_orders: number; lm_only_orders: number; ds_only_orders: number;
}
interface DailyData { days: number; rows: DailyRow[]; }

function DailyKpiReport() {
  const [d, setD] = useState<DailyData | null>(null);
  const [err, setErr] = useState('');
  const [days, setDays] = useState(30);
  useEffect(() => {
    client.get(ENDPOINTS.ai.report.daily, { params: { days } })
      .then((r) => setD(r.data?.data ?? null)).catch((e) => setErr(String(e)));
  }, [days]);
  if (err) return <Alert severity="error">{err}</Alert>;
  if (!d) return <Box sx={{ p: 4, textAlign: 'center' }}><CircularProgress /></Box>;
  const rows = d.rows;
  const latest = rows.length > 0 ? rows[rows.length - 1] : null;
  // 折线图：每日 total_pnl（蓝） vs ai_enhanced_pnl（绿）
  const dates = rows.map((r) => r.trade_date);
  const pnlLine = rows.map((r) => Number(r.total_pnl));
  const aiLine = rows.map((r) => Number(r.ai_enhanced_pnl));
  const lineOption = {
    tooltip: { trigger: 'axis' },
    legend: { data: ['总盈亏', 'AI 赋能单盈亏'], textStyle: { color: '#94a3b8' } },
    grid: { left: 50, right: 20, top: 40, bottom: 30 },
    xAxis: { type: 'category', data: dates, axisLabel: { color: '#94a3b8', rotate: 45 } },
    yAxis: { type: 'value', axisLabel: { color: '#94a3b8' }, splitLine: { lineStyle: { color: '#1e293b' } } },
    series: [
      { name: '总盈亏', type: 'line', data: pnlLine, itemStyle: { color: '#3b82f6' }, areaStyle: { opacity: 0.1 } },
      { name: 'AI 赋能单盈亏', type: 'line', data: aiLine, itemStyle: { color: '#22c55e' }, areaStyle: { opacity: 0.1 } },
    ],
  };
  // 饼图：AI 赋能 vs 非 AI 单盈亏占比（基于 latest）
  const pieOption = latest ? {
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { color: '#94a3b8' } },
    series: [{
      type: 'pie', radius: ['40%', '70%'],
      data: [
        { name: 'AI 赋能单盈亏', value: Number(latest.ai_enhanced_pnl), itemStyle: { color: '#22c55e' } },
        { name: '非 AI 单盈亏', value: Number(latest.non_ai_pnl), itemStyle: { color: '#64748b' } },
      ],
    }],
  } : null;
  return (
    <Box>
      <Stack direction="row" spacing={2} sx={{ mb: 2 }} flexWrap="wrap" useFlexGap alignItems="center">
        <Typography variant="subtitle2" sx={{ color: '#94a3b8' }}>回溯天数：</Typography>
        {[7, 30, 90, 180].map((n) => (
          <Button key={n} size="small" variant={days === n ? 'contained' : 'outlined'}
            onClick={() => setDays(n)} sx={{ color: days === n ? '#fff' : '#94a3b8' }}>
            {n} 天
          </Button>
        ))}
      </Stack>
      {latest && (
        <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap sx={{ mb: 2 }}>
          <KpiCard title="当日总盈亏" value={`$${fmt(latest.total_pnl)}`} color={Number(latest.total_pnl) >= 0 ? '#22c55e' : '#ef4444'} subtitle={`${latest.trade_date} · ${latest.total_orders} 单`} />
          <KpiCard title="AI 赋能单盈亏" value={`$${fmt(latest.ai_enhanced_pnl)}`} color={Number(latest.ai_enhanced_pnl) >= 0 ? '#22c55e' : '#ef4444'} subtitle={`${latest.ai_enhanced_orders} 单赋能`} />
          <KpiCard title="AI 贡献占比" value={latest.ai_contrib_ratio > 0 ? `${fmt(latest.ai_contrib_ratio * 100, 1)}%` : '—'} color="#8b5cf6" subtitle="AI 赋能单盈亏 / 总盈亏" />
          <KpiCard title="LM 推理" value={latest.lm_inferences} color="#3b82f6" subtitle="sidecar 推理次数" />
          <KpiCard title="AI 否决" value={latest.ai_vetoed} color="#ef4444" subtitle="VETO 信号数" />
          <KpiCard title="AI 赋能打开" value={latest.ai_opened} color="#22c55e" subtitle="hexp 未放行 → AI 打开" />
        </Stack>
      )}
      <Box sx={{ mt: 2, height: 300 }}>
        {rows.length > 0
          ? <ReactEChartsCore echarts={echarts} option={lineOption} style={{ height: 300 }} />
          : <Typography sx={{ color: '#64748b' }}>暂无每日 KPI 数据（后台聚合任务运行后积累）</Typography>}
      </Box>
      <Box sx={{ mt: 2, height: 260 }}>
        {pieOption && rows.length > 0
          ? <ReactEChartsCore echarts={echarts} option={pieOption} style={{ height: 260 }} />
          : <Typography sx={{ color: '#64748b' }}>暂无盈亏占比数据</Typography>}
      </Box>
      <Typography variant="subtitle2" sx={{ mt: 3, mb: 1, color: '#94a3b8' }}>每日 KPI 明细（最近 {days} 天）</Typography>
      <TableContainer component={Paper} elevation={0} sx={{ backgroundColor: '#111118', border: '1px solid #2a2a3a' }}>
        <Table size="small">
          <TableHead><TableRow>
            {['日期', '总单', '总盈亏', 'AI单', 'AI盈亏', 'AI占比', 'LM推理', '否决', '赋能打开', '融合'].map((h) => (
              <TableCell key={h} sx={{ color: '#94a3b8' }}>{h}</TableCell>
            ))}
          </TableRow></TableHead>
          <TableBody>
            {rows.map((r) => (
              <TableRow key={r.trade_date}>
                <TableCell sx={{ color: '#e2e8f0' }}>{r.trade_date}</TableCell>
                <TableCell sx={{ color: '#e2e8f0' }}>{r.total_orders}</TableCell>
                <TableCell sx={{ color: Number(r.total_pnl) >= 0 ? '#22c55e' : '#ef4444' }}>${fmt(r.total_pnl)}</TableCell>
                <TableCell sx={{ color: '#e2e8f0' }}>{r.ai_enhanced_orders}</TableCell>
                <TableCell sx={{ color: Number(r.ai_enhanced_pnl) >= 0 ? '#22c55e' : '#ef4444' }}>${fmt(r.ai_enhanced_pnl)}</TableCell>
                <TableCell sx={{ color: '#8b5cf6' }}>{r.ai_contrib_ratio > 0 ? `${fmt(r.ai_contrib_ratio * 100, 1)}%` : '—'}</TableCell>
                <TableCell sx={{ color: '#e2e8f0' }}>{r.lm_inferences}</TableCell>
                <TableCell sx={{ color: '#ef4444' }}>{r.ai_vetoed}</TableCell>
                <TableCell sx={{ color: '#22c55e' }}>{r.ai_opened}</TableCell>
                <TableCell sx={{ color: '#94a3b8' }}>f{r.fused_orders}/lm{r.lm_only_orders}/ds{r.ds_only_orders}</TableCell>
              </TableRow>
            ))}
            {rows.length === 0 && <TableRow><TableCell colSpan={10} sx={{ color: '#64748b' }}>暂无数据（后台聚合任务运行后积累，或历史回溯窗口内无成交）</TableCell></TableRow>}
          </TableBody>
        </Table>
      </TableContainer>
    </Box>
  );
}

/* ── 主容器 ─────────────────────────────────────────────────────── */
const TABS = [
  { key: 'health', label: '系统健康监控' },
  { key: 'layer', label: '信号分层统计' },
  { key: 'performance', label: '交易绩效对比' },
  { key: 'snapshot', label: 'AI 快照明细' },
  { key: 'daily', label: '每日 KPI（AI 盈亏贡献）' },
];

export default function AiReport() {
  const [tab, setTab] = useState(0);
  return (
    <Box sx={{ p: 3 }}>
      <Typography variant="h5" sx={{ mb: 2, color: '#f1f5f9' }}>AI 报表</Typography>
      <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ mb: 3, '.MuiTab-root': { color: '#94a3b8' }, '.Mui-selected': { color: '#3b82f6' } }}>
        {TABS.map((t) => <Tab key={t.key} label={t.label} />)}
      </Tabs>
      {tab === 0 && <HealthReport />}
      {tab === 1 && <LayerReport />}
      {tab === 2 && <PerformanceReport />}
      {tab === 3 && <SnapshotReport />}
      {tab === 4 && <DailyKpiReport />}
    </Box>
  );
}
