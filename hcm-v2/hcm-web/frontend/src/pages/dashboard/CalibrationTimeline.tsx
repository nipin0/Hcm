import React, { useEffect, useState, useMemo } from 'react';
import {
  Box, Typography, Paper, Button, ButtonGroup, Table, TableHead, TableBody,
  TableRow, TableCell, CircularProgress, Chip, Tabs, Tab,
} from '@mui/material';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import * as echarts from 'echarts/core';
import { LineChart, BarChart } from 'echarts/charts';
import {
  TooltipComponent, LegendComponent, GridComponent, MarkLineComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import client from '../../api/client';

echarts.use([LineChart, BarChart, TooltipComponent, LegendComponent, GridComponent, MarkLineComponent, CanvasRenderer]);

type Metric = 'calib' | 'win_rate';
type View = 'daily' | 'hexp';

interface LatestPerRegime { win_rate: number; calib_factor: number; trades: number; }
interface CalibRow {
  report_date: string; regime: string; trades: number; wins: number; losses: number;
  win_rate: number; calib_factor: number; sample_days: number; cold_start: boolean; applied: boolean;
}
interface AiDiagnosis {
  report_date: string;
  content: string;
  recommendations: string[];
  needs_review: boolean;
  confidence: string;
  model: string;
  created_at: string;
}
interface CalibData {
  regimes: string[];
  dates: string[];
  series: Record<string, { calib: number[]; win_rate: number[] }>;
  rows: CalibRow[];
  ai_diagnosis: AiDiagnosis | null;
  latest: {
    report_date: string;
    per_regime: Record<string, LatestPerRegime>;
    total_trades: number;
    sample_days: number;
    diagnosis: string;
    ai_diagnosis: AiDiagnosis | null;
  } | null;
}

// ── Hexp 信号质量时序 ──
interface HexpPoint {
  created_at: string; symbol: string; direction: string;
  hp_score: number; grade: string; verdict: number; k_value: number;
  co_dir: string; co_agree: boolean; co_pre_score: number; signal_mode: string;
}
interface HexpQualityData {
  summary: {
    total: number; hp_score_mean: number; hp_score_p50: number; hp_score_p90: number;
    grade_distribution: Record<string, number>;
    direction_distribution: Record<string, number>;
    co_agree_rate: number; co_agree_n: number;
  };
  series: HexpPoint[];
}

const GRADE_COLOR: Record<string, string> = {
  S: '#22c55e', A: '#84cc16', B: '#38bdf8', C: '#f59e0b', RED: '#ef4444', NONE: '#64748b',
};
const DIR_COLOR: Record<string, string> = { BUY: '#ef4444', SELL: '#22c55e', NO_TRADE: '#64748b' };

const METRIC_LABEL: Record<Metric, string> = { calib: '校准因子', win_rate: '胜率' };
const REGIME_COLOR: Record<string, string> = {
  NEUTRAL: '#38bdf8', TREND: '#22c55e', TREND_FADE: '#a855f7', RANGE: '#f97316',
};

const CalibrationTimeline: React.FC = () => {
  const [view, setView] = useState<View>('daily');
  const [metric, setMetric] = useState<Metric>('calib');
  const [data, setData] = useState<CalibData | null>(null);
  const [hexp, setHexp] = useState<HexpQualityData | null>(null);
  const [hexpHours, setHexpHours] = useState<number>(24);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string>('');

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const { data: resp } = await client.get('/api/v1/signal-tower/calibration-history');
      if (resp && resp.code === 0 && resp.data) {
        setData(resp.data as CalibData);
        setError('');
      } else {
        setError((resp && resp.message) || '无法加载校准时序数据');
      }
    } catch (e: any) {
      setError(e?.message || '加载失败');
    } finally {
      setLoading(false);
    }
  };

  const loadHexp = async () => {
    setLoading(true);
    setError('');
    try {
      const { data: resp } = await client.get(
        `/api/v1/signal-tower/hexp-quality?hours=${hexpHours}`,
      );
      if (resp && resp.code === 0 && resp.data) {
        setHexp(resp.data as HexpQualityData);
        setError('');
      } else {
        setError((resp && resp.message) || '无法加载和乘幂信号质量数据');
      }
    } catch (e: any) {
      setError(e?.message || '加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);
  useEffect(() => { if (view === 'hexp') loadHexp(); /* eslint-disable-next-line */ }, [view, hexpHours]);

  // ── 每日校准图 ──
  const chartOption = useMemo(() => {
    if (!data) return {};
    const isCalib = metric === 'calib';
    const yMin = isCalib ? 0.5 : 0;
    const yMax = isCalib ? 1.5 : 1;
    return {
      tooltip: { trigger: 'axis', valueFormatter: (v: number) => (isCalib ? v.toFixed(3) : `${(v * 100).toFixed(1)}%`) },
      legend: { data: data.regimes, textStyle: { color: '#cbd5e1' }, top: 4 },
      grid: { left: 56, right: 24, top: 44, bottom: 64 },
      xAxis: {
        type: 'category', data: data.dates, boundaryGap: false,
        axisLabel: { color: '#94a3b8', rotate: 45, fontSize: 10 },
        axisLine: { lineStyle: { color: '#334155' } },
      },
      yAxis: {
        type: 'value', min: yMin, max: yMax,
        axisLabel: {
          color: '#94a3b8',
          formatter: (v: number) => (isCalib ? v.toFixed(2) : `${(v * 100).toFixed(0)}%`),
        },
        splitLine: { lineStyle: { color: '#1e293b' } },
      },
      series: data.regimes.map((reg) => ({
        name: reg,
        type: 'line',
        smooth: true,
        showSymbol: true,
        symbolSize: 5,
        connectNulls: true,
        lineStyle: { width: 2, color: REGIME_COLOR[reg] || '#94a3b8' },
        itemStyle: { color: REGIME_COLOR[reg] || '#94a3b8' },
        data: (data.series[reg]?.[metric] || []),
        ...(isCalib ? {
          markLine: {
            silent: true, symbol: 'none',
            lineStyle: { color: '#64748b', type: 'dashed' },
            data: [{ yAxis: 1.0, label: { formatter: '基准 1.0', color: '#94a3b8', fontSize: 10 } }],
          },
        } : {}),
      })),
    };
  }, [data, metric]);

  // ── Hexp 质量图：hp_score 时序 + 分位线 ──
  const hexpChartOption = useMemo(() => {
    if (!hexp) return {};
    const xs = hexp.series.map((p) => p.created_at.slice(5, 16));
    const hp = hexp.series.map((p) => p.hp_score);
    return {
      tooltip: { trigger: 'axis', valueFormatter: (v: number) => (v == null ? '-' : v.toFixed(2)) },
      legend: { data: ['hp_score', '中线(p50)', 'P90'], textStyle: { color: '#cbd5e1' }, top: 4 },
      grid: { left: 48, right: 24, top: 44, bottom: 56 },
      xAxis: {
        type: 'category', data: xs, boundaryGap: false,
        axisLabel: { color: '#94a3b8', rotate: 45, fontSize: 10 },
        axisLine: { lineStyle: { color: '#334155' } },
      },
      yAxis: {
        type: 'value', min: 0, max: 100,
        axisLabel: { color: '#94a3b8', formatter: (v: number) => v.toFixed(0) },
        splitLine: { lineStyle: { color: '#1e293b' } },
      },
      series: [
        {
          name: 'hp_score', type: 'line', smooth: false, showSymbol: false, connectNulls: true,
          lineStyle: { width: 1.5, color: '#38bdf8' }, itemStyle: { color: '#38bdf8' }, data: hp,
        },
        {
          name: '中线(p50)', type: 'line', showSymbol: false,
          lineStyle: { color: '#64748b', type: 'dashed', width: 1 }, data: xs.map(() => hexp.summary.hp_score_p50),
        },
        {
          name: 'P90', type: 'line', showSymbol: false,
          lineStyle: { color: '#f59e0b', type: 'dotted', width: 1 }, data: xs.map(() => hexp.summary.hp_score_p90),
        },
      ],
    };
  }, [hexp]);

  // ── Hexp 评级分布（柱图） ──
  const hexpGradeOption = useMemo(() => {
    if (!hexp) return {};
    const entries = Object.entries(hexp.summary.grade_distribution).sort(
      (a, b) => (GRADE_COLOR[a[0]] ? 1 : 0) - (GRADE_COLOR[b[0]] ? 1 : 0),
    );
    return {
      tooltip: { trigger: 'axis' },
      grid: { left: 40, right: 16, top: 16, bottom: 28 },
      xAxis: {
        type: 'category', data: entries.map((e) => e[0]),
        axisLabel: { color: '#94a3b8', fontSize: 11 }, axisLine: { lineStyle: { color: '#334155' } },
      },
      yAxis: { type: 'value', axisLabel: { color: '#94a3b8' }, splitLine: { lineStyle: { color: '#1e293b' } } },
      series: [{
        type: 'bar', data: entries.map((e) => ({
          value: e[1], itemStyle: { color: GRADE_COLOR[e[0]] || '#64748b' },
        })),
        barWidth: '55%',
      }],
    };
  }, [hexp]);

  // ── Hexp 方向分布（柱图） ──
  const hexpDirOption = useMemo(() => {
    if (!hexp) return {};
    const entries = Object.entries(hexp.summary.direction_distribution);
    return {
      tooltip: { trigger: 'axis' },
      grid: { left: 40, right: 16, top: 16, bottom: 28 },
      xAxis: {
        type: 'category', data: entries.map((e) => e[0]),
        axisLabel: { color: '#94a3b8', fontSize: 11 }, axisLine: { lineStyle: { color: '#334155' } },
      },
      yAxis: { type: 'value', axisLabel: { color: '#94a3b8' }, splitLine: { lineStyle: { color: '#1e293b' } } },
      series: [{
        type: 'bar', data: entries.map((e) => ({
          value: e[1], itemStyle: { color: DIR_COLOR[e[0]] || '#64748b' },
        })),
        barWidth: '55%',
      }],
    };
  }, [hexp]);

  const sortedRows = useMemo(() => {
    if (!data) return [];
    return [...data.rows].sort((a, b) =>
      a.report_date === b.report_date
        ? a.regime.localeCompare(b.regime)
        : b.report_date.localeCompare(a.report_date));
  }, [data]);

  const hexpSorted = useMemo(() => {
    if (!hexp) return [];
    return [...hexp.series].sort((a, b) => b.created_at.localeCompare(a.created_at));
  }, [hexp]);

  return (
    <Box>
      <Box className="flex items-center justify-between flex-wrap gap-3 mb-4">
        <Typography variant="h5" className="text-slate-100 font-semibold">校准时序 · AI 自我发展轨迹</Typography>
        <Box className="flex items-center gap-3">
          <Tabs
            value={view}
            onChange={(_, v) => setView(v)}
            textColor="primary"
            indicatorColor="primary"
            sx={{ minHeight: 32 }}
          >
            <Tab label="每日校准" value="daily" sx={{ minHeight: 32, py: 0.5 }} />
            <Tab label="和乘幂信号质量" value="hexp" sx={{ minHeight: 32, py: 0.5 }} />
          </Tabs>
          <Button size="small" variant="outlined" onClick={view === 'hexp' ? loadHexp : load}>刷新</Button>
        </Box>
      </Box>

      {view === 'daily' && (
        <>
          <Typography variant="caption" className="text-slate-500">
            每日按 M5 体制累计胜率→校准因子（公式与本地校准器一致：clamp(1.0+(胜率-0.5),0.6,1.4)）。
            因子&lt;1 表示该系统在该体制上历史胜率偏低、信号被压制；&gt;1 表示可信度更高、信号被放大。
          </Typography>

          {loading && <Box className="flex justify-center py-10"><CircularProgress /></Box>}
          {error && <Typography color="error" className="mb-3 mt-2">{error}</Typography>}

          {data && !loading && (
            <Box className="flex flex-col gap-4 mt-3">
              {data.latest && (
                <Paper className="p-4 bg-gray-800 border-l-4 border-blue-500">
                  <Box className="flex items-center justify-between mb-2">
                    <Typography variant="subtitle2" className="text-slate-200">
                      最新校准报告 · {data.latest.report_date}
                      {data.latest.sample_days > 0 && (
                        <Typography component="span" variant="caption" className="text-slate-400 ml-2">
                          （{data.latest.sample_days} 个样本日 / {data.latest.total_trades} 笔已平仓）
                        </Typography>
                      )}
                    </Typography>
                    <Chip size="small" label="当前生效" color="info" variant="outlined" />
                  </Box>
                  <Typography variant="body2" className="text-slate-300 leading-relaxed">
                    {data.latest.diagnosis}
                  </Typography>
                  <Box className="flex gap-2 flex-wrap mt-3">
                    {Object.entries(data.latest.per_regime).map(([reg, v]) => (
                      <Chip
                        key={reg}
                        size="small"
                        variant="outlined"
                        label={`${reg} 胜率 ${(v.win_rate * 100).toFixed(1)}% / 因子 ${v.calib_factor.toFixed(2)}`}
                        sx={{ color: REGIME_COLOR[reg] || '#94a3b8', borderColor: REGIME_COLOR[reg] || '#94a3b8' }}
                      />
                    ))}
                  </Box>
                </Paper>
              )}

              {data.ai_diagnosis && (
                <Paper className="p-4 bg-gray-800 border-l-4 border-purple-500">
                  <Box className="flex items-center justify-between mb-2 flex-wrap gap-2">
                    <Typography variant="subtitle2" className="text-slate-200">
                      AI 自然语言诊断 · {data.ai_diagnosis.report_date}
                      <Typography component="span" variant="caption" className="text-slate-400 ml-2">
                        （DeepSeek · {data.ai_diagnosis.model}）
                      </Typography>
                    </Typography>
                    <Box className="flex gap-2">
                      <Chip
                        size="small"
                        label={`置信度 ${data.ai_diagnosis.confidence}`}
                        color={data.ai_diagnosis.confidence === 'high' ? 'success' : data.ai_diagnosis.confidence === 'medium' ? 'warning' : 'default'}
                        variant="outlined"
                      />
                      {data.ai_diagnosis.needs_review && (
                        <Chip size="small" label="需人工复核" color="error" variant="outlined" />
                      )}
                    </Box>
                  </Box>
                  <Typography variant="body2" className="text-slate-300 leading-relaxed whitespace-pre-line">
                    {data.ai_diagnosis.content}
                  </Typography>
                  {data.ai_diagnosis.recommendations.length > 0 && (
                    <Box className="mt-3">
                      <Typography variant="caption" className="text-slate-400">调参建议</Typography>
                      <ul className="mt-1 pl-5 space-y-1">
                        {data.ai_diagnosis.recommendations.map((rec, i) => (
                          <li key={i} className="text-slate-300 text-sm">{rec}</li>
                        ))}
                      </ul>
                    </Box>
                  )}
                  <Typography variant="caption" className="text-slate-500 mt-2 block">
                    生成于 {data.ai_diagnosis.created_at}
                  </Typography>
                </Paper>
              )}

              <Paper className="p-3 bg-gray-800">
                <Box className="flex items-center justify-between mb-1">
                  <Typography variant="subtitle2" className="text-slate-300">按体制演化（截至每日累计）</Typography>
                  <ButtonGroup size="small" variant="outlined">
                    {(['calib', 'win_rate'] as Metric[]).map((m) => (
                      <Button key={m} variant={metric === m ? 'contained' : 'outlined'} onClick={() => setMetric(m)}>
                        {METRIC_LABEL[m]}
                      </Button>
                    ))}
                  </ButtonGroup>
                </Box>
                {data.dates.length === 0 ? (
                  <Typography variant="body2" className="text-slate-500 py-10 text-center">
                    尚无校准快照：标注样本不足或每日校准任务尚未运行。
                  </Typography>
                ) : (
                  <ReactEChartsCore echarts={echarts} option={chartOption} style={{ height: 360 }} notMerge />
                )}
              </Paper>

              <Paper className="p-3 bg-gray-800">
                <Typography variant="subtitle2" className="text-slate-300 mb-2">每日校准明细</Typography>
                <Box className="overflow-x-auto">
                  <Table size="small">
                    <TableHead>
                      <TableRow>
                        <TableCell className="text-slate-400">交易日</TableCell>
                        <TableCell className="text-slate-400">体制</TableCell>
                        <TableCell className="text-slate-400">笔数</TableCell>
                        <TableCell className="text-slate-400">胜</TableCell>
                        <TableCell className="text-slate-400">负</TableCell>
                        <TableCell className="text-slate-400">当日胜率</TableCell>
                        <TableCell className="text-slate-400">累计校准因子</TableCell>
                        <TableCell className="text-slate-400">状态</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {sortedRows.map((r, i) => (
                        <TableRow key={i}>
                          <TableCell className="text-slate-300 whitespace-nowrap">{r.report_date}</TableCell>
                          <TableCell style={{ color: REGIME_COLOR[r.regime] || '#94a3b8' }}>{r.regime}</TableCell>
                          <TableCell className="text-slate-300">{r.trades}</TableCell>
                          <TableCell className="text-slate-300">{r.wins}</TableCell>
                          <TableCell className="text-slate-300">{r.losses}</TableCell>
                          <TableCell className="text-slate-300">{(r.win_rate * 100).toFixed(1)}%</TableCell>
                          <TableCell className="text-slate-300">{r.calib_factor.toFixed(3)}</TableCell>
                          <TableCell>
                            {r.applied
                              ? <Chip size="small" label="生效" color="info" variant="outlined" />
                              : r.cold_start
                                ? <Chip size="small" label="冷启动" color="warning" variant="outlined" />
                                : <Chip size="small" label="历史" variant="outlined" />}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </Box>
              </Paper>
            </Box>
          )}
        </>
      )}

      {view === 'hexp' && (
        <>
          <Box className="flex items-center gap-3 mb-2 flex-wrap">
            <Typography variant="caption" className="text-slate-500">
              和乘幂(hexp)模型信号质量随时间演变：hp_score 评分、评级(grade)、共振 verdict、方向，
              并叠加影子模拟胜率（SL/TP 命中评估）。红色评级(RED)信号被拦截、不进入交易。
            </Typography>
            <ButtonGroup size="small" variant="outlined">
              {[24, 72, 168, 720].map((h) => (
                <Button key={h} variant={hexpHours === h ? 'contained' : 'outlined'} onClick={() => setHexpHours(h)}>
                  {h >= 720 ? '30天' : h >= 168 ? '7天' : h >= 72 ? '3天' : '24h'}
                </Button>
              ))}
            </ButtonGroup>
          </Box>

          {loading && <Box className="flex justify-center py-10"><CircularProgress /></Box>}
          {error && <Typography color="error" className="mb-3 mt-2">{error}</Typography>}

          {hexp && !loading && (
            <Box className="flex flex-col gap-4 mt-2">
              {/* 汇总卡片 */}
              <Box className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <Paper className="p-3 bg-gray-800">
                  <Typography variant="caption" className="text-slate-400">信号总数</Typography>
                  <Typography variant="h6" className="text-slate-100">{hexp.summary.total}</Typography>
                </Paper>
                <Paper className="p-3 bg-gray-800">
                  <Typography variant="caption" className="text-slate-400">hp_score 均值 / P50 / P90</Typography>
                  <Typography variant="h6" className="text-slate-100">
                    {hexp.summary.hp_score_mean.toFixed(1)}
                    <Typography component="span" variant="caption" className="text-slate-400 ml-1">
                      / {hexp.summary.hp_score_p50.toFixed(0)} / {hexp.summary.hp_score_p90.toFixed(0)}
                    </Typography>
                  </Typography>
                </Paper>
                <Paper className="p-3 bg-gray-800">
                  <Typography variant="caption" className="text-slate-400">与 co_source 一致率</Typography>
                  <Typography variant="h6" className="text-slate-100">
                    {(hexp.summary.co_agree_rate * 100).toFixed(1)}%
                    <Typography component="span" variant="caption" className="text-slate-400 ml-1">
                      ({hexp.summary.co_agree_n}/{hexp.summary.total})
                    </Typography>
                  </Typography>
                </Paper>
              </Box>

              {hexp.summary.total === 0 ? (
                <Paper className="p-10 bg-gray-800">
                  <Typography variant="body2" className="text-slate-500 text-center">
                    窗口内无 hexp 信号落库。当前生产模型若为 hexp，信号会写入 signal_mode='hexp'；
                    若处于影子对照模式，写入 signal_mode='hexp_shadow'。请确认 hcm_signal.signals 有对应记录。
                  </Typography>
                </Paper>
              ) : (
                <>
                  <Paper className="p-3 bg-gray-800">
                    <Typography variant="subtitle2" className="text-slate-300 mb-1">hp_score 时序（含 P50/P90 分位线）</Typography>
                    <ReactEChartsCore echarts={echarts} option={hexpChartOption} style={{ height: 320 }} notMerge />
                  </Paper>

                  <Box className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <Paper className="p-3 bg-gray-800">
                      <Typography variant="subtitle2" className="text-slate-300 mb-1">评级(grade)分布</Typography>
                      <ReactEChartsCore echarts={echarts} option={hexpGradeOption} style={{ height: 220 }} notMerge />
                    </Paper>
                    <Paper className="p-3 bg-gray-800">
                      <Typography variant="subtitle2" className="text-slate-300 mb-1">方向分布</Typography>
                      <ReactEChartsCore echarts={echarts} option={hexpDirOption} style={{ height: 220 }} notMerge />
                    </Paper>
                  </Box>

                  <Paper className="p-3 bg-gray-800">
                    <Typography variant="subtitle2" className="text-slate-300 mb-2">信号明细（最新优先）</Typography>
                    <Box className="overflow-x-auto">
                      <Table size="small">
                        <TableHead>
                          <TableRow>
                            <TableCell className="text-slate-400">时间</TableCell>
                            <TableCell className="text-slate-400">品种</TableCell>
                            <TableCell className="text-slate-400">方向</TableCell>
                            <TableCell className="text-slate-400">hp_score</TableCell>
                            <TableCell className="text-slate-400">评级</TableCell>
                            <TableCell className="text-slate-400">verdict</TableCell>
                            <TableCell className="text-slate-400">K</TableCell>
                            <TableCell className="text-slate-400">co_dir</TableCell>
                            <TableCell className="text-slate-400">与co一致</TableCell>
                            <TableCell className="text-slate-400">模式</TableCell>
                          </TableRow>
                        </TableHead>
                        <TableBody>
                          {hexpSorted.slice(0, 60).map((p, i) => (
                            <TableRow key={i}>
                              <TableCell className="text-slate-300 whitespace-nowrap">{p.created_at.slice(5, 16)}</TableCell>
                              <TableCell className="text-slate-300">{p.symbol}</TableCell>
                              <TableCell style={{ color: DIR_COLOR[p.direction] || '#94a3b8' }}>{p.direction}</TableCell>
                              <TableCell className="text-slate-300">{p.hp_score.toFixed(1)}</TableCell>
                              <TableCell>
                                <Chip size="small" label={p.grade} sx={{ color: GRADE_COLOR[p.grade] || '#94a3b8', borderColor: GRADE_COLOR[p.grade] || '#94a3b8' }} variant="outlined" />
                              </TableCell>
                              <TableCell className="text-slate-300">{p.verdict.toFixed(2)}</TableCell>
                              <TableCell className="text-slate-300">{p.k_value.toFixed(2)}</TableCell>
                              <TableCell className="text-slate-300">{p.co_dir || '-'}</TableCell>
                              <TableCell className="text-slate-300">{p.co_agree ? '✓' : '✗'}</TableCell>
                              <TableCell className="text-slate-400">{p.signal_mode}</TableCell>
                            </TableRow>
                          ))}
                        </TableBody>
                      </Table>
                    </Box>
                  </Paper>
                </>
              )}
            </Box>
          )}
        </>
      )}
    </Box>
  );
};

export default CalibrationTimeline;
