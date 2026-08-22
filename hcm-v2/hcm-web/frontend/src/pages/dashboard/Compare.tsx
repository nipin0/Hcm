import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { Box, Typography, LinearProgress } from '@mui/material';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import * as echarts from 'echarts/core';
import { HeatmapChart } from 'echarts/charts';
import { GridComponent, TooltipComponent, VisualMapComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import client from '../../api/client';
import { ENDPOINTS } from '../../api/endpoints';

echarts.use([HeatmapChart, GridComponent, TooltipComponent, VisualMapComponent, CanvasRenderer]);

interface CompareData {
  symbols: string[];
  metrics: string[];
  data: number[][]; // [symbol_idx][metric_idx]
  min: number;
  max: number;
}

const Compare: React.FC = () => {
  const [data, setData] = useState<CompareData | null>(null);
  const [loading, setLoading] = useState<boolean>(false);

  const fetchData = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const { data: resData } = await client.get(ENDPOINTS.dashboard.symbolsCompare);
      const payload = resData.data || resData; // 解包 {code, data, message} 封装层
      if (payload && payload.symbols && Array.isArray(payload.symbols)) {
        // 后端返回对象数组 [{symbol, total_trades, wins, ...}]，映射为前端 CompareData 格式
        const symbols = payload.symbols.map((s: any) => s.symbol);
        const metrics = ['交易次数', '胜场', '败场', '胜率%', '总盈亏', '均盈亏', '均手数', '总佣金'];
        const data2d: number[][] = payload.symbols.map((s: any) => [
          s.total_trades ?? 0,
          s.wins ?? 0,
          s.losses ?? 0,
          s.win_rate ?? 0,
          s.total_profit ?? 0,
          s.avg_profit_per_trade ?? 0,
          s.avg_lot ?? 0,
          s.total_commission ?? 0,
        ]);
        const allVals = data2d.flat().filter((v) => typeof v === 'number');
        setData({
          symbols,
          metrics,
          data: data2d,
          min: allVals.length > 0 ? Math.min(...allVals) : 0,
          max: allVals.length > 0 ? Math.max(...allVals) : 100,
        });
      } else {
        setData(null);
      }
    } catch {
      setData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const heatmapOption = useMemo(() => {
    if (!data) return {};

    const seriesData: [number, number, number][] = [];
    for (let i = 0; i < data.symbols.length; i++) {
      for (let j = 0; j < data.metrics.length; j++) {
        seriesData.push([j, i, data.data[i]?.[j] ?? 0]);
      }
    }

    return {
      backgroundColor: 'transparent',
      grid: { left: 120, right: 60, top: 10, bottom: 40 },
      tooltip: {
        backgroundColor: '#1a1a24',
        borderColor: '#2a2a3a',
        textStyle: { color: '#f1f5f9', fontSize: 12 },
        formatter: (params: { value: [number, number, number] }) => {
          if (!params.value) return '';
          const [metricIdx, symbolIdx, val] = params.value;
          const symbol = data.symbols[symbolIdx];
          const metric = data.metrics[metricIdx];
          return `<strong>${symbol}</strong><br/>${metric}: ${val.toFixed(2)}`;
        },
      },
      xAxis: {
        type: 'category' as const,
        data: data.metrics,
        axisLine: { lineStyle: { color: '#2a2a3a' } },
        axisLabel: { color: '#94a3b8', fontSize: 11, rotate: 30 },
        position: 'bottom' as const,
      },
      yAxis: {
        type: 'category' as const,
        data: data.symbols,
        axisLine: { lineStyle: { color: '#2a2a3a' } },
        axisLabel: { color: '#94a3b8', fontSize: 11, fontWeight: 600 },
      },
      visualMap: {
        min: data.min,
        max: data.max,
        calculable: true,
        orient: 'vertical' as const,
        right: 0,
        top: 'center',
        textStyle: { color: '#94a3b8', fontSize: 10 },
        inRange: {
          color: ['#1e3a5f', '#3b82f6', '#22c55e', '#eab308', '#ef4444'],
        },
      },
      series: [{
        type: 'heatmap',
        data: seriesData,
        label: {
          show: true,
          color: '#f1f5f9',
          fontSize: 11,
          fontWeight: 600,
        },
        emphasis: {
          itemStyle: {
            shadowBlur: 10,
            shadowColor: 'rgba(0,0,0,0.5)',
          },
        },
      }],
    };
  }, [data]);

  if (loading && !data) {
    return <LinearProgress sx={{ backgroundColor: '#1a1a24', '& .MuiLinearProgress-bar': { backgroundColor: '#3b82f6' } }} />;
  }

  return (
    <Box>
      <Typography variant="h6" className="text-gray-100 font-semibold mb-2">品种对比</Typography>
      <Typography variant="body2" className="text-gray-500 mb-6">
        多品种多维指标对比热力图 — 颜色越深表示指标值越高
      </Typography>

      {data ? (
        <Box className="card" style={{ height: Math.max(400, data.symbols.length * 50 + 100) }}>
          <ReactEChartsCore
            echarts={echarts}
            option={heatmapOption}
            style={{ height: '100%', minHeight: 400 }}
            notMerge
            lazyUpdate
          />
        </Box>
      ) : (
        <Box className="card">
          <Typography className="text-gray-500 text-center py-12">暂无对比数据</Typography>
        </Box>
      )}

      {/* Legend */}
      {data && (
        <Box className="grid grid-cols-2 md:grid-cols-4 gap-3 mt-4">
          {data.symbols.slice(0, 8).map((sym, idx) => (
            <Box key={sym} className="card py-3 px-4">
              <Typography variant="caption" className="text-gray-400 uppercase tracking-wider">{sym}</Typography>
              <Box className="mt-2 space-y-1">
                {data.metrics.slice(0, 4).map((metric, mIdx) => (
                  <Box key={metric} className="flex justify-between text-xs">
                    <span className="text-gray-500">{metric}</span>
                    <span className="text-gray-300 font-medium">{data.data[idx]?.[mIdx]?.toFixed(2) ?? '--'}</span>
                  </Box>
                ))}
              </Box>
            </Box>
          ))}
        </Box>
      )}
    </Box>
  );
};

export default Compare;
