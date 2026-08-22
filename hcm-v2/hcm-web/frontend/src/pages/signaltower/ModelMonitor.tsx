import React, { useEffect, useState } from 'react';
import {
  Box, Paper, Typography, Table, TableBody, TableCell,
  TableContainer, TableHead, TableRow, Alert, CircularProgress, Chip, Stack,
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

/** 模型监控报表页 — P3-A monitoring_report.py 落库的四视图（PSI/校准/置信/行情环境）。只读。 */

interface PsiData {
  max: number;
  mean: number;
  drifted_features: string[];
  per_feature: Record<string, number>;
}
interface CalBucket { bin: string; count: number; mean_ai: number | null; pass_rate: number | null; }
interface ConfHist { bin: string; count: number; pct: number; }
interface RegimeDist { direction: Record<string, number>; grade: Record<string, number>; }
interface MonitorLatest {
  generated_at: string;
  window_hours: number;
  n_samples: number;
  psi: PsiData;
  calibration_buckets: CalBucket[];
  confidence_hist: ConfHist[];
  regime_distribution: RegimeDist;
}
interface MonitorData { latest: MonitorLatest | null; history: MonitorLatest[]; }

const PSI_WARN = 0.25; // 标准 PSI 重度漂移阈值

function fmt(v: number | null | undefined, digits = 2): string {
  return v === null || v === undefined ? '—' : Number(v).toFixed(digits);
}
function psiColor(v: number): string {
  if (v > PSI_WARN) return '#ef4444';
  if (v > 0.1) return '#f59e0b';
  return '#22c55e';
}
const REGIME_COLOR: Record<string, string> = { TREND: '#22c55e', NEUTRAL: '#3b82f6', RANGE: '#f59e0b', UNKNOWN: '#64748b', BUY: '#22c55e', SELL: '#ef4444', HOLD: '#64748b' };

export default function ModelMonitor() {
  const [d, setD] = useState<MonitorData | null>(null);
  const [err, setErr] = useState('');
  const [updatedAt, setUpdatedAt] = useState<string>('');

  useEffect(() => {
    const load = () => {
      client.get(ENDPOINTS.ai.report.monitor)
        .then((r) => {
          const payload = r.data?.data ?? null;
          setD(payload);
          setUpdatedAt(new Date().toLocaleTimeString('zh-CN', { hour12: false }));
        })
        .catch((e) => setErr(String(e)));
    };
    load();
    const timer = setInterval(load, 30000); // 每 30s 刷新
    return () => clearInterval(timer);
  }, []);

  if (err) return <Alert severity="error">{err}</Alert>;
  if (!d) return <Box sx={{ p: 4, textAlign: 'center' }}><CircularProgress /></Box>;

  const latest = d.latest;
  if (!latest) {
    return (
      <Box sx={{ p: 3 }}>
        <Typography variant="h5" sx={{ mb: 2, color: '#f1f5f9' }}>模型监控（PSI / 漂移）</Typography>
        <Alert severity="info">监控数据积累中 — monitoring_report.py 尚未落库（守护循环每次重训判定后写 hcm:ai:monitor:report:latest）。稍后自动刷新。</Alert>
      </Box>
    );
  }

  const psi = latest.psi;
  const drifted = psi.drifted_features ?? [];
  const psiWarn = psi.max > PSI_WARN;

  // PSI 柱图（每特征）
  const psiFeats = Object.keys(psi.per_feature ?? {});
  const psiBarOption = {
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 120, right: 30, top: 20, bottom: 30 },
    xAxis: { type: 'value', axisLabel: { color: '#94a3b8' }, splitLine: { lineStyle: { color: '#1e293b' } } },
    yAxis: { type: 'category', data: psiFeats.slice().reverse(), axisLabel: { color: '#94a3b8', fontSize: 10 } },
    series: [{
      type: 'bar',
      data: psiFeats.slice().reverse().map((f) => ({ value: psi.per_feature[f], itemStyle: { color: psiColor(psi.per_feature[f]) } })),
      label: { show: true, position: 'right', color: '#94a3b8', formatter: (p: any) => Number(p.value).toFixed(4) },
      markLine: {
        silent: true, symbol: 'none',
        data: [{ xAxis: PSI_WARN, name: '重度漂移', lineStyle: { color: '#ef4444', type: 'dashed' }, label: { color: '#ef4444', formatter: '0.25' } }],
      },
    }],
  };

  // 校准分桶（bin × pass_rate，tooltip 带 count/mean_ai）
  const cal = latest.calibration_buckets ?? [];
  const calBins = cal.map((c) => c.bin);
  const calPassRate = cal.map((c) => (c.pass_rate === null ? null : c.pass_rate * 100));
  const calMeanAi = cal.map((c) => c.mean_ai);
  const calCount = cal.map((c) => c.count);
  const calOption = {
    tooltip: {
      trigger: 'axis',
      formatter: (params: any) => {
        const i = params[0].dataIndex;
        return `${calBins[i]}<br/>通过率: ${calPassRate[i] === null ? '—' : calPassRate[i].toFixed(1) + '%'}<br/>均值AI: ${calMeanAi[i] ?? '—'}<br/>样本: ${calCount[i]}`;
      },
    },
    legend: { data: ['通过率', '均值AI'], textStyle: { color: '#94a3b8' } },
    grid: { left: 50, right: 50, top: 40, bottom: 30 },
    xAxis: { type: 'category', data: calBins, axisLabel: { color: '#94a3b8' } },
    yAxis: [
      { type: 'value', name: '通过率%', min: 0, max: 100, axisLabel: { color: '#94a3b8' }, splitLine: { lineStyle: { color: '#1e293b' } } },
      { type: 'value', name: '均值AI', min: 0, max: 100, axisLabel: { color: '#94a3b8' }, splitLine: { show: false } },
    ],
    series: [
      { name: '通过率', type: 'bar', data: calPassRate, itemStyle: { color: '#22c55e' } },
      { name: '均值AI', type: 'line', yAxisIndex: 1, data: calMeanAi, itemStyle: { color: '#3b82f6' }, smooth: true },
    ],
  };

  // 置信分布直方图（count + pct）
  const conf = latest.confidence_hist ?? [];
  const confOption = {
    tooltip: {
      trigger: 'axis',
      formatter: (params: any) => {
        const i = params[0].dataIndex;
        return `${conf[i].bin}<br/>样本: ${conf[i].count}<br/>占比: ${(conf[i].pct * 100).toFixed(1)}%`;
      },
    },
    grid: { left: 50, right: 20, top: 30, bottom: 30 },
    xAxis: { type: 'category', data: conf.map((c) => c.bin), axisLabel: { color: '#94a3b8', fontSize: 10 } },
    yAxis: [
      { type: 'value', name: '样本数', axisLabel: { color: '#94a3b8' }, splitLine: { lineStyle: { color: '#1e293b' } } },
      { type: 'value', name: '占比%', axisLabel: { color: '#94a3b8' }, splitLine: { show: false } },
    ],
    series: [
      { type: 'bar', data: conf.map((c) => c.count), itemStyle: { color: '#8b5cf6' }, name: '样本数' },
      { type: 'line', yAxisIndex: 1, data: conf.map((c) => c.pct * 100), itemStyle: { color: '#eab308' }, name: '占比%', smooth: true },
    ],
  };

  // 行情环境分布（direction / grade 两个饼）
  const reg = latest.regime_distribution ?? { direction: {}, grade: {} };
  const dirData = Object.entries(reg.direction ?? {}).map(([k, v]) => ({ name: k, value: v, itemStyle: { color: REGIME_COLOR[k] ?? '#64748b' } }));
  const gradeData = Object.entries(reg.grade ?? {}).map(([k, v]) => ({ name: k, value: v, itemStyle: { color: REGIME_COLOR[k] ?? '#64748b' } }));
  const dirPie = {
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { color: '#94a3b8' } },
    series: [{ type: 'pie', radius: ['40%', '70%'], data: dirData.length ? dirData : [{ name: '无数据', value: 1, itemStyle: { color: '#334155' } }] }],
  };
  const gradePie = {
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, textStyle: { color: '#94a3b8' } },
    series: [{ type: 'pie', radius: ['40%', '70%'], data: gradeData.length ? gradeData : [{ name: '无数据', value: 1, itemStyle: { color: '#334155' } }] }],
  };

  return (
    <Box sx={{ p: 3 }}>
      <Stack direction="row" spacing={2} alignItems="center">
        <Typography variant="h5" sx={{ color: '#f1f5f9' }}>模型监控（PSI / 漂移）</Typography>
        <Chip size="small" label={psiWarn ? `PSI 重度漂移 max=${fmt(psi.max, 4)}` : `PSI 正常 max=${fmt(psi.max, 4)}`}
          sx={{ backgroundColor: psiWarn ? '#ef444422' : '#22c55e22', color: psiWarn ? '#f87171' : '#34d399' }} />
        {updatedAt && <Typography variant="caption" sx={{ color: '#64748b' }}>刷新于 {updatedAt}（每 30s）</Typography>}
      </Stack>

      {psiWarn && <Alert severity="warning" sx={{ my: 2 }}>检测到特征分布重度漂移（PSI max &gt; {PSI_WARN}），建议复核监控报表或触发重训判定。</Alert>}

      {/* KPI 卡 */}
      <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap sx={{ mt: 2 }}>
        <KpiCard title="窗口样本数" value={latest.n_samples} color="#3b82f6" subtitle={`最近 ${latest.window_hours}h`} />
        <KpiCard title="PSI 最大" value={fmt(psi.max, 4)} color={psiColor(psi.max)} subtitle={`重度阈值 ${PSI_WARN}`} />
        <KpiCard title="PSI 均值" value={fmt(psi.mean, 4)} color={psiColor(psi.mean)} subtitle="全特征均值" />
        <KpiCard title="漂移特征数" value={drifted.length} color={drifted.length > 0 ? '#ef4444' : '#22c55e'} subtitle="超过阈值特征数" />
      </Stack>

      {latest.generated_at && <Typography variant="caption" sx={{ color: '#64748b', display: 'block', mt: 1 }}>报表生成：{String(latest.generated_at).replace('T', ' ').slice(0, 19)}（UTC）</Typography>}

      {/* ① PSI 滑动窗口 */}
      <Typography variant="subtitle2" sx={{ mt: 3, mb: 1, color: '#94a3b8' }}>① PSI 特征分布漂移（vs live 推理基线）</Typography>
      <Box sx={{ height: Math.max(240, psiFeats.length * 22) }}>
        {psiFeats.length > 0
          ? <ReactEChartsCore echarts={echarts} option={psiBarOption} style={{ height: '100%' }} notMerge />
          : <Typography sx={{ color: '#64748b' }}>无特征 PSI 数据</Typography>}
      </Box>
      {drifted.length > 0 && (
        <Alert severity="warning" sx={{ mt: 1 }}>漂移特征：{drifted.join(', ')}</Alert>
      )}

      {/* ①b 漂移特征明细表 */}
      <Typography variant="subtitle2" sx={{ mt: 2, mb: 1, color: '#94a3b8' }}>PSI 明细（按值降序）</Typography>
      <TableContainer component={Paper} elevation={0} sx={{ backgroundColor: '#111118', border: '1px solid #2a2a3a' }}>
        <Table size="small">
          <TableHead><TableRow>
            {['特征', 'PSI', '状态'].map((h) => <TableCell key={h} sx={{ color: '#94a3b8' }}>{h}</TableCell>)}
          </TableRow></TableHead>
          <TableBody>
            {psiFeats.sort((a, b) => psi.per_feature[b] - psi.per_feature[a]).map((f) => (
              <TableRow key={f}>
                <TableCell sx={{ color: '#e2e8f0' }}>{f}</TableCell>
                <TableCell sx={{ color: psiColor(psi.per_feature[f]) }}>{fmt(psi.per_feature[f], 4)}</TableCell>
                <TableCell><Chip size="small" label={psi.per_feature[f] > PSI_WARN ? '重度漂移' : psi.per_feature[f] > 0.1 ? '关注' : '正常'}
                  sx={{ backgroundColor: psiColor(psi.per_feature[f]) + '22', color: psiColor(psi.per_feature[f]) }} /></TableCell>
              </TableRow>
            ))}
            {psiFeats.length === 0 && <TableRow><TableCell colSpan={3} sx={{ color: '#64748b' }}>暂无可展示特征</TableCell></TableRow>}
          </TableBody>
        </Table>
      </TableContainer>

      {/* ② 校准分桶 */}
      <Typography variant="subtitle2" sx={{ mt: 3, mb: 1, color: '#94a3b8' }}>② 校准分桶（ai_score 十分位 × 通过率）</Typography>
      <Box sx={{ height: 300 }}>
        {cal.length > 0
          ? <ReactEChartsCore echarts={echarts} option={calOption} style={{ height: 300 }} notMerge />
          : <Typography sx={{ color: '#64748b' }}>暂无校准数据</Typography>}
      </Box>

      {/* ③ 置信分布 */}
      <Typography variant="subtitle2" sx={{ mt: 3, mb: 1, color: '#94a3b8' }}>③ 置信分布（ai_score 直方图）</Typography>
      <Box sx={{ height: 260 }}>
        {conf.length > 0
          ? <ReactEChartsCore echarts={echarts} option={confOption} style={{ height: 260 }} notMerge />
          : <Typography sx={{ color: '#64748b' }}>暂无置信分布数据</Typography>}
      </Box>

      {/* ④ 行情环境分布 */}
      <Typography variant="subtitle2" sx={{ mt: 3, mb: 1, color: '#94a3b8' }}>④ 行情环境分布（direction / grade 占比）</Typography>
      <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap>
        <Box sx={{ flex: '1 1 320px', height: 280 }}>
          <Typography variant="caption" sx={{ color: '#94a3b8' }}>方向占比</Typography>
          <ReactEChartsCore echarts={echarts} option={dirPie} style={{ height: 260 }} notMerge />
        </Box>
        <Box sx={{ flex: '1 1 320px', height: 280 }}>
          <Typography variant="caption" sx={{ color: '#94a3b8' }}>评级占比</Typography>
          <ReactEChartsCore echarts={echarts} option={gradePie} style={{ height: 260 }} notMerge />
        </Box>
      </Stack>
    </Box>
  );
}
