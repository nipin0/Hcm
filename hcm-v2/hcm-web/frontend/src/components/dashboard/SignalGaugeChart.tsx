import React, { useMemo } from 'react';
import { Box, Typography, Paper } from '@mui/material';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import * as echarts from 'echarts/core';
import { BarChart } from 'echarts/charts';
import { GridComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { FactorGauge, SignalGaugeSummary } from '../../types/signalGauge';
import { DEFAULT_FACTORS, DEFAULT_SUMMARY } from '../../types/signalGauge';
import SignalSummaryBar from './SignalSummaryBar';

echarts.use([BarChart, GridComponent, TooltipComponent, CanvasRenderer]);

interface SignalGaugeChartProps {
  factors: FactorGauge[];
  summary: SignalGaugeSummary;
}

const FACTOR_LABEL_NAMES: Record<string, string> = {
  ma_alignment: 'MA对齐',
  macd: 'MACD',
  adx: 'ADX',
  boll: '布林带',
  stoch: 'KD',
  rsi: 'RSI',
  bar_momentum: '单Bar动量',
  boll_vol: '布林波幅',
  ao: 'AO',
  // Legacy fallback keys (kept for backward compatibility)
  MACD: 'MACD',
  MA5: 'MA5',
  MA20: 'MA20',
  CCI: 'CCI',
  ATR: 'ATR',
  ADX: 'ADX',
  AO: 'AO',
};

const FACTOR_FORMAT: Record<string, (v: number) => string> = {
  // Scoring-engine raw scores are in [-1, +1]
  ma_alignment: (v) => v.toFixed(2),
  macd: (v) => v.toFixed(2),
  adx: (v) => v.toFixed(2),
  boll: (v) => v.toFixed(2),
  stoch: (v) => v.toFixed(2),
  rsi: (v) => v.toFixed(2),
  bar_momentum: (v) => v.toFixed(2),
  boll_vol: (v) => v.toFixed(2),
  ao: (v) => v.toFixed(2),
  // Legacy keys
  MACD: (v) => v.toFixed(1),
  MA5: (v) => v.toFixed(1),
  MA20: (v) => v.toFixed(1),
  CCI: (v) => v.toFixed(0),
  ATR: (v) => v.toFixed(1),
  ADX: (v) => v.toFixed(0),
  AO: (v) => v.toFixed(1),
};

/** ECharts stacked bar chart for 7-factor signal visualization */
const SignalGaugeChart: React.FC<SignalGaugeChartProps> = ({
  factors,
  summary,
}) => {
  // Use API data when it has any factors; fall back to DEFAULT only when API returns empty
  const effectiveFactors: FactorGauge[] = factors.length > 0 ? factors : DEFAULT_FACTORS;
  const effectiveSummary: SignalGaugeSummary = factors.length > 0 ? summary : DEFAULT_SUMMARY;

  const option = useMemo(() => {
    const shortData: (number | null)[] = effectiveFactors.map((f) =>
      f.is_auxiliary ? null : f.short_pct
    );
    const neutralData: (number | null)[] = effectiveFactors.map((f) =>
      f.is_auxiliary ? null : f.neutral_pct
    );
    const longData: (number | null)[] = effectiveFactors.map((f) =>
      f.is_auxiliary ? null : f.long_pct
    );
    const auxData: (number | null)[] = effectiveFactors.map((f) =>
      f.is_auxiliary ? 100 : null
    );

    return {
      backgroundColor: 'transparent',
      grid: {
        left: 10,
        right: 10,
        top: 20,
        bottom: 40,
        containLabel: false,
      },
      tooltip: {
        trigger: 'axis' as const,
        axisPointer: { type: 'shadow' as const },
        backgroundColor: '#1a1a24',
        borderColor: '#2a2a3a',
        textStyle: { color: '#f1f5f9', fontSize: 12 },
        formatter: (params: Array<{ seriesName: string; value: number | null; dataIndex: number }>) => {
          const idx: number = params[0]?.dataIndex ?? 0;
          const f: FactorGauge | undefined = effectiveFactors[idx];
          if (!f) return '';

          const lines: string[] = [
            `<strong>${FACTOR_LABEL_NAMES[f.key] || f.key}</strong>`,
            `权重: ${(f.weight * 100).toFixed(1)}%`,
          ];

          if (f.raw_detail) {
            lines.push(`<span style="color:#94a3b8">真实读数: ${f.raw_detail}</span>`);
          } else {
            lines.push(`原始分: ${f.raw_value}`);
          }

          if (f.is_auxiliary) {
            lines.push('<span style="color:#38bdf8">辅助指标：无多空方向</span>');
          } else {
            for (const p of params) {
              if (p.value !== null && p.value !== undefined) {
                lines.push(`${p.seriesName}: ${p.value}%`);
              }
            }
          }
          return lines.join('<br/>');
        },
      },
      xAxis: {
        type: 'category' as const,
        data: effectiveFactors.map((f) => {
          const name = FACTOR_LABEL_NAMES[f.key] || f.key;
          // Show the REAL raw reading under the bar (e.g. "ADX 12.5"),
          // falling back to the derived score when raw_display is absent.
          const val = f.raw_display ?? (() => {
            const fmt = FACTOR_FORMAT[f.key];
            return fmt ? fmt(f.raw_value) : f.raw_value.toFixed(2);
          })();
          return `${name}\n${val}`;
        }),
        axisLine: { lineStyle: { color: '#2a2a3a' } },
        axisLabel: {
          color: '#94a3b8',
          fontSize: 10,
          rotate: 0,
          interval: 0,
          formatter: (value: string) => value,
        },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value' as const,
        min: 0,
        max: 100,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { show: false },
        splitLine: { show: false },
      },
      series: [
        {
          name: '看空',
          type: 'bar' as const,
          stack: 'signal',
          data: shortData,
          itemStyle: {
            color: '#ef4444',
            borderRadius: [4, 4, 0, 0],
          },
          barWidth: 32,
          emphasis: { itemStyle: { color: '#f87171' } },
        },
        {
          name: '中性',
          type: 'bar' as const,
          stack: 'signal',
          data: neutralData,
          itemStyle: { color: '#eab308' },
          barWidth: 32,
          emphasis: { itemStyle: { color: '#facc15' } },
        },
        {
          name: '看多',
          type: 'bar' as const,
          stack: 'signal',
          data: longData,
          itemStyle: {
            color: '#22c55e',
            borderRadius: [0, 0, 4, 4],
          },
          barWidth: 32,
          emphasis: { itemStyle: { color: '#4ade80' } },
        },
        {
          name: '辅助指标',
          type: 'bar' as const,
          stack: 'signal',
          data: auxData,
          itemStyle: {
            color: '#38bdf8',
            borderRadius: [4, 4, 4, 4],
          },
          barWidth: 32,
          emphasis: { itemStyle: { color: '#7dd3fc' } },
        },
      ],
    };
  }, [effectiveFactors]);

  return (
    <Paper
      elevation={0}
      sx={{
        backgroundColor: '#111118',
        border: '1px solid #2a2a3a',
        borderRadius: 3,
        p: 2.5,
      }}
    >
      {/* Top header: title + summary badges */}
      <Box className="flex items-center justify-between mb-2">
        <Typography sx={{ color: '#f1f5f9', fontSize: 15, fontWeight: 700 }}>
          AI 多空信号仪表盘
        </Typography>
        <SignalSummaryBar summary={effectiveSummary} variant="top" />
      </Box>

      {/* ECharts stacked bar chart */}
      <Box style={{ height: 300 }}>
        <ReactEChartsCore
          echarts={echarts}
          option={option}
          style={{ height: '100%', width: '100%' }}
          notMerge
          lazyUpdate
        />
      </Box>

      {/* Bottom legend */}
      <SignalSummaryBar summary={effectiveSummary} variant="bottom" />
    </Paper>
  );
};

export default SignalGaugeChart;
