import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { Box, Typography, Chip, LinearProgress } from '@mui/material';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import * as echarts from 'echarts/core';
import { LineChart, BarChart } from 'echarts/charts';
import { GridComponent, TooltipComponent, LegendComponent, DataZoomComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import client from '../../api/client';
import { ENDPOINTS } from '../../api/endpoints';
import { useSymbol } from '../../contexts/SymbolContext';

echarts.use([LineChart, BarChart, GridComponent, TooltipComponent, LegendComponent, DataZoomComponent, CanvasRenderer]);

interface Position {
  id: string;
  symbol: string;
  direction: string;
  volume: number;
  open_price: number;
  current_price: number;
  pnl: number;
  pnl_percent: number;
  open_time: string;
  stop_loss: number;
  take_profit: number;
}

interface EquityCurve {
  timestamp: string;
  equity: number;
  balance: number;
}

const Positions: React.FC = () => {
  const { selectedSymbol } = useSymbol();
  const [positions, setPositions] = useState<Position[]>([]);
  const [equityCurve, setEquityCurve] = useState<EquityCurve[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [totalPnl, setTotalPnl] = useState<number>(0);

  const fetchData = useCallback(async (): Promise<void> => {
    if (!selectedSymbol) return;
    setLoading(true);
    try {
      // Positions — primary endpoint
      const posRes = await client.get(
        `${ENDPOINTS.positions.list}?symbol=${selectedSymbol.symbol}`,
      );
      setPositions(posRes.data.positions || []);
      setTotalPnl(posRes.data.total_pnl || 0);

      // Equity-curve — endpoint not yet fully implemented; 404 is expected
      try {
        const eqRes = await client.get(
          `${ENDPOINTS.dashboard.equityCurve}?symbol=${selectedSymbol.symbol}`,
        );
        setEquityCurve((eqRes.data?.data?.curve || eqRes.data?.curve || []));
      } catch {
        setEquityCurve([]);
      }
    } catch {
      setPositions([]);
      setEquityCurve([]);
    } finally {
      setLoading(false);
    }
  }, [selectedSymbol]);

  useEffect(() => {
    fetchData();
    const handler = (): void => { fetchData(); };
    window.addEventListener('symbolChanged', handler);
    return () => window.removeEventListener('symbolChanged', handler);
  }, [fetchData]);

  const equityOption = useMemo(() => ({
    backgroundColor: 'transparent',
    grid: { left: 50, right: 20, top: 20, bottom: 30 },
    tooltip: {
      trigger: 'axis' as const,
      backgroundColor: '#1a1a24',
      borderColor: '#2a2a3a',
      textStyle: { color: '#f1f5f9', fontSize: 12 },
    },
    xAxis: {
      type: 'category' as const,
      data: equityCurve.map((e) => e.timestamp),
      axisLine: { lineStyle: { color: '#2a2a3a' } },
      axisLabel: { color: '#64748b', fontSize: 10 },
    },
    yAxis: {
      type: 'value' as const,
      axisLine: { lineStyle: { color: '#2a2a3a' } },
      axisLabel: { color: '#64748b', fontSize: 10 },
      splitLine: { lineStyle: { color: '#1a1a24' } },
    },
    series: [
      {
        name: '净值',
        type: 'line',
        data: equityCurve.map((e) => e.equity),
        smooth: true,
        symbol: 'none',
        areaStyle: {
          color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
            { offset: 0, color: 'rgba(59,130,246,0.3)' },
            { offset: 1, color: 'rgba(59,130,246,0.02)' },
          ]),
        },
        lineStyle: { color: '#3b82f6', width: 2 },
      },
      {
        name: '余额',
        type: 'line',
        data: equityCurve.map((e) => e.balance),
        smooth: true,
        symbol: 'none',
        lineStyle: { color: '#8b5cf6', width: 1.5, type: 'dashed' as const },
      },
    ],
    legend: {
      data: ['净值', '余额'],
      textStyle: { color: '#94a3b8', fontSize: 11 },
      top: 0,
    },
  }), [equityCurve]);

  return (
    <Box>
      <Box className="flex items-center justify-between mb-4">
        <Typography variant="h6" className="text-gray-100 font-semibold">仓位与盈亏</Typography>
        <Box className="flex items-center gap-3">
          <Typography variant="body2" className="text-gray-400">
            总盈亏: <span className={totalPnl >= 0 ? 'text-green-400 font-bold' : 'text-red-400 font-bold'}>
              {totalPnl >= 0 ? '+' : ''}{totalPnl.toFixed(2)} USD
            </span>
          </Typography>
        </Box>
      </Box>

      {/* Equity Curve */}
      <Box className="card mb-4" style={{ height: 300 }}>
        <Typography variant="caption" className="text-gray-400 uppercase tracking-wider block mb-1">
          净值曲线
        </Typography>
        {equityCurve.length > 0 ? (
          <ReactEChartsCore
            echarts={echarts}
            option={equityOption}
            style={{ height: 260 }}
            notMerge
            lazyUpdate
          />
        ) : (
          <Box className="flex items-center justify-center h-64">
            <Typography className="text-gray-500">暂无数据</Typography>
          </Box>
        )}
      </Box>

      {/* Positions Table */}
      <Box className="card">
        <Typography variant="caption" className="text-gray-400 uppercase tracking-wider block mb-3">
          持仓列表 ({positions.length})
        </Typography>
        {loading ? (
          <LinearProgress sx={{ backgroundColor: '#1a1a24', '& .MuiLinearProgress-bar': { backgroundColor: '#3b82f6' } }} />
        ) : positions.length === 0 ? (
          <Typography className="text-gray-500 text-center py-8">暂无持仓</Typography>
        ) : (
          <Box className="overflow-x-auto">
            <table className="w-full">
              <thead>
                <tr className="border-b border-gray-700">
                  <th className="text-left py-2 px-3 text-xs text-gray-400">品种</th>
                  <th className="text-left py-2 px-3 text-xs text-gray-400">方向</th>
                  <th className="text-right py-2 px-3 text-xs text-gray-400">手数</th>
                  <th className="text-right py-2 px-3 text-xs text-gray-400">开仓价</th>
                  <th className="text-right py-2 px-3 text-xs text-gray-400">现价</th>
                  <th className="text-right py-2 px-3 text-xs text-gray-400">盈亏</th>
                  <th className="text-right py-2 px-3 text-xs text-gray-400">盈亏%</th>
                  <th className="text-right py-2 px-3 text-xs text-gray-400">止损</th>
                  <th className="text-right py-2 px-3 text-xs text-gray-400">止盈</th>
                </tr>
              </thead>
              <tbody>
                {positions.map((pos) => (
                  <tr key={pos.id} className="border-b border-gray-800 hover:bg-gray-800/30 transition-colors">
                    <td className="py-2 px-3 text-sm text-gray-200 font-medium">{pos.symbol}</td>
                    <td className="py-2 px-3">
                      <Chip
                        label={pos.direction}
                        size="small"
                        sx={{
                          backgroundColor: pos.direction === 'BUY' ? '#0a2e1a' : '#3b1111',
                          color: pos.direction === 'BUY' ? '#22c55e' : '#ef4444',
                          fontSize: 10, height: 20, fontWeight: 600,
                        }}
                      />
                    </td>
                    <td className="py-2 px-3 text-sm text-right text-gray-300">{pos.volume.toFixed(2)}</td>
                    <td className="py-2 px-3 text-sm text-right text-gray-300">{pos.open_price.toFixed(5)}</td>
                    <td className="py-2 px-3 text-sm text-right text-gray-300">{pos.current_price.toFixed(5)}</td>
                    <td className={`py-2 px-3 text-sm text-right font-medium ${pos.pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {pos.pnl >= 0 ? '+' : ''}{pos.pnl.toFixed(2)}
                    </td>
                    <td className={`py-2 px-3 text-sm text-right ${pos.pnl_percent >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {pos.pnl_percent >= 0 ? '+' : ''}{pos.pnl_percent.toFixed(2)}%
                    </td>
                    <td className="py-2 px-3 text-sm text-right text-gray-400">{pos.stop_loss.toFixed(5)}</td>
                    <td className="py-2 px-3 text-sm text-right text-gray-400">{pos.take_profit.toFixed(5)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Box>
        )}
      </Box>
    </Box>
  );
};

export default Positions;
