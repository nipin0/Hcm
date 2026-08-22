import React, { useEffect, useState, useCallback } from 'react';
import {
  Box, Paper, Typography, Table, TableBody, TableCell,
  TableContainer, TableHead, TableRow, Alert, CircularProgress,
  Chip, Stack, Button, Card, CardContent, Divider,
} from '@mui/material';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import * as echarts from 'echarts/core';
import { LineChart, BarChart } from 'echarts/charts';
import { GridComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import client from '../../api/client';
import { ENDPOINTS } from '../../api/endpoints';

echarts.use([LineChart, BarChart, GridComponent, TooltipComponent, CanvasRenderer]);

/** 自动重训守护报表 — LightGBM 运行状况 + DeepSeek 裁判总结。只读。 */

interface JudgeInfo {
  decision: string;       // adopt | rollback | local_fallback
  reason: string;
}
interface RetrainRun {
  model_version?: string;
  auc?: number | null;
  samples?: number | null;
  ds_nonzero_ratio?: number | null;
  baseline_win_rate?: number | null;
  adopted?: boolean;
  switched?: boolean;
  at?: string;
  judge?: JudgeInfo | null;
}
interface SummaryData {
  daemon_online: boolean;
  daemon_info: { pid: number; at: string } | null;
  current_model: string | null;
  current_calib: string | null;
  last_run: RetrainRun | null;
  last_run_at: string | null;
  history: RetrainRun[];
  summary_kpis: {
    total_rounds: number;
    adopt_count: number;
    rollback_count: number;
    local_fallback_count: number;
    latest_auc: number | null;
    auc_trend: (number | null)[];
  };
}

const judgeColor: Record<string, string> = {
  adopt: '#16a34a',
  rollback: '#dc2626',
  local_fallback: '#64748b',
};
const judgeLabel: Record<string, string> = {
  adopt: '采纳（切换模型）',
  rollback: '回滚（不切换）',
  local_fallback: '本地护栏',
};

function DecisionChip({ decision }: { decision?: string }) {
  const key = decision || 'local_fallback';
  const color = judgeColor[key] || '#64748b';
  return (
    <Chip
      size="small"
      label={judgeLabel[key] || key}
      sx={{ backgroundColor: color, color: '#fff', fontSize: 11, height: 22 }}
    />
  );
}

function ModelReport() {
  const [data, setData] = useState<SummaryData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await client.get(ENDPOINTS.signalTower.retrainSummary);
      const body = (resp as any).data ?? resp;
      if (body && body.code === 0 && body.data) {
        setData(body.data as SummaryData);
      } else {
        setError((body && body.message) || '加载失败');
      }
    } catch (e: any) {
      setError(e?.message || String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [load]);

  if (loading && !data) {
    return (
      <Box className="flex items-center justify-center h-64">
        <CircularProgress size={28} />
      </Box>
    );
  }

  const kpis = data?.summary_kpis;
  const aucTrend = (kpis?.auc_trend || []).filter((x) => x != null).map((x) => x as number);

  return (
    <Box className="space-y-4">
      <Typography variant="h5" className="text-slate-100 font-semibold">
        模型重训报表
      </Typography>
      <Typography variant="body2" className="text-slate-400">
        LightGBM 自动重训守护的运行状况与 DeepSeek 裁判总结（每 30 秒自动刷新）
      </Typography>

      {error && <Alert severity="error">{error}</Alert>}

      {/* ── LightGBM 运行状况 ── */}
      <Paper className="p-4 bg-gray-800/60 border border-gray-700">
        <Typography variant="h6" className="text-slate-200 mb-3">
          LightGBM 运行状况
        </Typography>
        <Stack direction="row" spacing={2} className="flex-wrap">
          <StatusCard
            title="重训守护"
            value={data?.daemon_online ? '在线' : '离线'}
            color={data?.daemon_online ? '#16a34a' : '#dc2626'}
            sub={data?.daemon_info ? `PID ${data.daemon_info.pid} · ${data.daemon_info.at}` : '未检测到心跳'}
          />
          <StatusCard
            title="当前线上模型"
            value={data?.current_model ? basename(data.current_model) : '—'}
            color="#3b82f6"
            sub={data?.current_calib ? `Calib: ${basename(data.current_calib)}` : '无校准器'}
          />
          <StatusCard
            title="最近一轮"
            value={data?.last_run?.model_version || '—'}
            color="#a855f7"
            sub={data?.last_run_at ? `时间 ${data.last_run_at}` : '尚无记录'}
          />
          <StatusCard
            title="累计轮数"
            value={String(kpis?.total_rounds ?? 0)}
            color="#eab308"
            sub={`采纳 ${kpis?.adopt_count ?? 0} · 回滚 ${kpis?.rollback_count ?? 0}`}
          />
          <StatusCard
            title="最新 AUC"
            value={kpis?.latest_auc != null ? kpis.latest_auc.toFixed(3) : '—'}
            color="#14b8a6"
            sub={data?.last_run ? `样本 ${data.last_run.samples ?? '—'}` : ''}
          />
        </Stack>

        {aucTrend.length > 0 && (
          <Box className="mt-4" style={{ height: 180 }}>
            <ReactEChartsCore
              echarts={echarts}
              option={{
                backgroundColor: 'transparent',
                tooltip: { trigger: 'axis' },
                grid: { left: 40, right: 16, top: 16, bottom: 24 },
                xAxis: {
                  type: 'category',
                  data: aucTrend.map((_, i) => `R${aucTrend.length - i}`),
                  axisLine: { lineStyle: { color: '#475569' } },
                  axisLabel: { color: '#94a3b8', fontSize: 10 },
                },
                yAxis: {
                  type: 'value',
                  scale: true,
                  axisLine: { lineStyle: { color: '#475569' } },
                  axisLabel: { color: '#94a3b8', fontSize: 10 },
                  splitLine: { lineStyle: { color: '#334155' } },
                },
                series: [
                  {
                    type: 'line',
                    data: aucTrend,
                    smooth: true,
                    symbolSize: 6,
                    lineStyle: { color: '#14b8a6', width: 2 },
                    itemStyle: { color: '#14b8a6' },
                    areaStyle: { color: 'rgba(20,184,166,0.12)' },
                  },
                ],
              }}
              notMerge
              style={{ height: '100%', width: '100%' }}
            />
          </Box>
        )}
      </Paper>

      {/* ── DeepSeek 裁判总结 ── */}
      <Paper className="p-4 bg-gray-800/60 border border-gray-700">
        <Box className="flex items-center justify-between mb-3">
          <Typography variant="h6" className="text-slate-200">
            DeepSeek 裁判总结
          </Typography>
          <Button size="small" variant="outlined" onClick={load} disabled={loading}>
            刷新
          </Button>
        </Box>
        {!data?.history || data.history.length === 0 ? (
          <Alert severity="info">暂无重训记录</Alert>
        ) : (
          <TableContainer>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell sx={{ color: '#94a3b8', fontSize: 12 }}>轮次</TableCell>
                  <TableCell sx={{ color: '#94a3b8', fontSize: 12 }}>模型版本</TableCell>
                  <TableCell sx={{ color: '#94a3b8', fontSize: 12 }}>AUC</TableCell>
                  <TableCell sx={{ color: '#94a3b8', fontSize: 12 }}>样本</TableCell>
                  <TableCell sx={{ color: '#94a3b8', fontSize: 12 }}>DS特征占比</TableCell>
                  <TableCell sx={{ color: '#94a3b8', fontSize: 12 }}>裁判</TableCell>
                  <TableCell sx={{ color: '#94a3b8', fontSize: 12 }}>切换</TableCell>
                  <TableCell sx={{ color: '#94a3b8', fontSize: 12 }}>理由</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {data.history.map((h, i) => (
                  <TableRow key={i} hover>
                    <TableCell sx={{ color: '#cbd5e1', fontSize: 12 }}>
                      #{data.history.length - i}
                    </TableCell>
                    <TableCell sx={{ color: '#cbd5e1', fontSize: 12 }}>
                      {h.model_version || '—'}
                    </TableCell>
                    <TableCell sx={{ color: '#cbd5e1', fontSize: 12 }}>
                      {h.auc != null ? h.auc.toFixed(3) : '—'}
                    </TableCell>
                    <TableCell sx={{ color: '#cbd5e1', fontSize: 12 }}>
                      {h.samples ?? '—'}
                    </TableCell>
                    <TableCell sx={{ color: '#cbd5e1', fontSize: 12 }}>
                      {h.ds_nonzero_ratio != null ? `${(h.ds_nonzero_ratio * 100).toFixed(0)}%` : '—'}
                    </TableCell>
                    <TableCell>
                      <DecisionChip decision={h.judge?.decision} />
                    </TableCell>
                    <TableCell sx={{ color: '#cbd5e1', fontSize: 12 }}>
                      {h.switched ? (
                        <Chip size="small" label="已切换" sx={{ backgroundColor: '#16a34a', color: '#fff', fontSize: 10, height: 20 }} />
                      ) : h.adopted ? (
                        <Chip size="small" label="已采纳" sx={{ backgroundColor: '#65a30d', color: '#fff', fontSize: 10, height: 20 }} />
                      ) : (
                        <span className="text-slate-500 text-xs">否</span>
                      )}
                    </TableCell>
                    <TableCell
                      sx={{
                        color: '#94a3b8',
                        fontSize: 11.5,
                        maxWidth: 360,
                        whiteSpace: 'normal',
                        wordBreak: 'break-word',
                      }}
                    >
                      {h.judge?.reason || '—'}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        )}
        {data?.last_run?.judge && (
          <Box className="mt-3 pt-3 border-t border-gray-700">
            <Typography variant="caption" className="text-slate-400">
              最新裁决：
            </Typography>
            <Box className="mt-1 flex items-start gap-2">
              <DecisionChip decision={data.last_run.judge.decision} />
              <Typography variant="body2" className="text-slate-300" sx={{ lineHeight: 1.5 }}>
                {data.last_run.judge.reason}
              </Typography>
            </Box>
          </Box>
        )}
      </Paper>
    </Box>
  );
}

function StatusCard({ title, value, color, sub }: { title: string; value: string; color: string; sub?: string }) {
  return (
    <Card className="bg-gray-800 border border-gray-700" sx={{ minWidth: 170, flex: '1 1 170px' }}>
      <CardContent sx={{ py: 1.5, '&:last-child': { pb: 1.5 } }}>
        <Typography variant="caption" className="text-slate-400">
          {title}
        </Typography>
        <Typography variant="h6" sx={{ color, fontWeight: 600, fontSize: 18, mt: 0.5 }}>
          {value}
        </Typography>
        {sub && (
          <Typography variant="caption" className="text-slate-500" sx={{ fontSize: 11 }}>
            {sub}
          </Typography>
        )}
      </CardContent>
    </Card>
  );
}

function basename(p: string): string {
  if (!p) return p;
  const parts = p.split(/[\\/]/);
  return parts[parts.length - 1] || p;
}

export default ModelReport;
