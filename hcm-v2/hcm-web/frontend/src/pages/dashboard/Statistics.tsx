import React, { useState, useEffect, useCallback } from 'react';
import {
  Box,
  Typography,
  LinearProgress,
  Chip,
} from '@mui/material';
import { LocalizationProvider, DatePicker } from '@mui/x-date-pickers';
import { AdapterDateFns } from '@mui/x-date-pickers/AdapterDateFnsV3';
import { format } from 'date-fns';
import client from '../../api/client';
import { ENDPOINTS } from '../../api/endpoints';
import { useSymbol } from '../../contexts/SymbolContext';
import { useSignalGauges, useRealtimeQuote } from '../../api/signalGauge';
import KpiCard from '../../components/KpiCard';
import RealtimeQuoteCard from '../../components/dashboard/RealtimeQuoteCard';
import SignalGaugeChart from '../../components/dashboard/SignalGaugeChart';
import ModelStatusPanel from '../../components/dashboard/ModelStatusPanel';

/** Minimal KPI data from the statistics endpoint */
interface KpiData {
  total_trades: number;
  win_rate: number;
  profit_factor: number;
  max_drawdown: number;
  avg_profit: number;
  avg_loss: number;
  sharpe_ratio: number;
  total_signals: number;
}

/** Common slotProps for date picker text fields (dark theme) */
const DATE_PICKER_SLOT_PROPS = {
  textField: {
    size: 'small' as const,
    sx: {
      '& .MuiOutlinedInput-root': { backgroundColor: '#1a1a24' },
      '& .MuiInputLabel-root': { color: '#94a3b8' },
      '& .MuiOutlinedInput-notchedOutline': { borderColor: '#334155' },
    },
  },
};

/** Compute a Date offset by N days from today */
const daysAgo = (n: number): Date =>
  new Date(Date.now() - n * 24 * 60 * 60 * 1000);

/** Format a duration in seconds to a human-readable age string (e.g. "刚刚", "5m30s", "2h") */
function formatSignalAge(isoStr: string | null | undefined): string {
  if (!isoStr) return '--';
  const now = Date.now();
  const then = new Date(isoStr).getTime();
  if (isNaN(then)) return '--';
  const diffSec = Math.floor((now - then) / 1000);
  if (diffSec < 5) return '刚刚';
  if (diffSec < 60) return `${diffSec}s`;
  const mins = Math.floor(diffSec / 60);
  const secs = diffSec % 60;
  if (mins < 60) return secs ? `${mins}m${secs}s` : `${mins}m`;
  const hours = Math.floor(mins / 60);
  const remMins = mins % 60;
  return remMins ? `${hours}h${remMins}m` : `${hours}h`;
}

const Statistics: React.FC = () => {
  const { selectedSymbol } = useSymbol();
  const symbol: string = selectedSymbol?.symbol ?? 'XAUUSD';

  // ── Date range state (default: last 30 days) ──
  const [startDate, setStartDate] = useState<Date | null>(daysAgo(30));
  const [endDate, setEndDate] = useState<Date | null>(new Date());

  // KPI data
  const [kpi, setKpi] = useState<KpiData | null>(null);
  const [kpiLoading, setKpiLoading] = useState<boolean>(false);

  // Force re-render every second so the displayed signal age stays live
  const [ageTick, setAgeTick] = useState<number>(0);
  useEffect(() => {
    const timer = setInterval(() => {
      setAgeTick((t) => t + 1);
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  // Signal gauge data
  const {
    factors,
    summary,
    latestInference,
    scoringThresholds,
    loading: gaugeLoading,
    error: gaugeError,
  } = useSignalGauges(symbol);

  // Realtime quote data
  const {
    quote,
    loading: quoteLoading,
    error: quoteError,
  } = useRealtimeQuote(symbol);

  const fetchKpi = useCallback(async (): Promise<void> => {
    if (!selectedSymbol) return;
    if (!startDate || !endDate) return;
    setKpiLoading(true);
    try {
      const start: string = format(startDate, 'yyyy-MM-dd');
      const end: string = format(endDate, 'yyyy-MM-dd');
      const { data } = await client.get(
        `${ENDPOINTS.dashboard.statistics}?symbol=${symbol}&start_date=${start}&end_date=${end}`,
      );
      const raw = data.data || data;
      if (raw) {
        setKpi({
          total_trades: raw.total_trades ?? 0,
          win_rate: raw.win_rate ?? 0,
          profit_factor: raw.profit_factor ?? 0,
          max_drawdown: raw.max_drawdown ?? 0,
          avg_profit: raw.avg_profit ?? 0,
          avg_loss: raw.avg_loss ?? 0,
          sharpe_ratio: raw.sharpe_ratio ?? 0,
          total_signals: raw.total_signals ?? 0,
        });
      }
    } catch {
      setKpi(null);
    } finally {
      setKpiLoading(false);
    }
  }, [selectedSymbol, symbol, startDate, endDate]);

  useEffect(() => {
    fetchKpi();
    const handler = (): void => {
      fetchKpi();
    };
    window.addEventListener('symbolChanged', handler);
    return () => window.removeEventListener('symbolChanged', handler);
  }, [fetchKpi]);

  // ── Quick-select handlers ──
  const handleQuickSelect = (preset: string): void => {
    const today: Date = new Date();
    let start: Date;
    switch (preset) {
      case '7d':
        start = daysAgo(7);
        break;
      case '30d':
        start = daysAgo(30);
        break;
      case 'month':
        start = new Date(today.getFullYear(), today.getMonth(), 1);
        break;
      default:
        return;
    }
    setStartDate(start);
    setEndDate(today);
  };

  const isLoading: boolean =
    (gaugeLoading && !factors.length) || (kpiLoading && !kpi);

  if (isLoading) {
    return (
      <LinearProgress
        sx={{
          backgroundColor: '#1a1a24',
          '& .MuiLinearProgress-bar': { backgroundColor: '#3b82f6' },
        }}
      />
    );
  }

  // ageTick is intentionally read by the root <Box data-...> attribute so the
  // 1s tick above forces re-render and the "信号年龄" cell stays live.
  return (
    <Box data-age-tick={ageTick}>
      <Typography variant="h6" className="text-gray-100 font-semibold mb-4">
        统计分析
      </Typography>

      {/* ── Date Range Selector ── */}
      <Box sx={{ mb: 3 }}>
        <LocalizationProvider dateAdapter={AdapterDateFns}>
          <Box
            sx={{
              display: 'flex',
              gap: 1.5,
              alignItems: 'center',
              flexWrap: 'wrap',
            }}
          >
            <DatePicker
              label="起始日期"
              value={startDate}
              onChange={(newValue: Date | null) => setStartDate(newValue)}
              format="yyyy-MM-dd"
              slotProps={DATE_PICKER_SLOT_PROPS}
            />
            <Typography sx={{ color: '#94a3b8', fontSize: '0.85rem' }}>
              —
            </Typography>
            <DatePicker
              label="结束日期"
              value={endDate}
              onChange={(newValue: Date | null) => setEndDate(newValue)}
              format="yyyy-MM-dd"
              slotProps={DATE_PICKER_SLOT_PROPS}
            />

            {/* Quick-select chips */}
            <Chip
              label="最近7天"
              size="small"
              onClick={() => handleQuickSelect('7d')}
              sx={{
                backgroundColor: '#1a1a24',
                color: '#94a3b8',
                fontSize: '0.75rem',
                fontWeight: 500,
                cursor: 'pointer',
                '&:hover': { backgroundColor: '#252540', color: '#e2e8f0' },
              }}
            />
            <Chip
              label="最近30天"
              size="small"
              onClick={() => handleQuickSelect('30d')}
              sx={{
                backgroundColor: '#1a1a24',
                color: '#94a3b8',
                fontSize: '0.75rem',
                fontWeight: 500,
                cursor: 'pointer',
                '&:hover': { backgroundColor: '#252540', color: '#e2e8f0' },
              }}
            />
            <Chip
              label="本月"
              size="small"
              onClick={() => handleQuickSelect('month')}
              sx={{
                backgroundColor: '#1a1a24',
                color: '#94a3b8',
                fontSize: '0.75rem',
                fontWeight: 500,
                cursor: 'pointer',
                '&:hover': { backgroundColor: '#252540', color: '#e2e8f0' },
              }}
            />
          </Box>
        </LocalizationProvider>
      </Box>

      {/* ── 4-Block KPI Cards Row ── */}
      <Box className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4 mb-3">
        {/* Block 1 — 风险与质量 */}
        <KpiCard
          title="风险与质量"
          value=""
          items={[
            { label: '胜率', value: kpi ? `${kpi.win_rate.toFixed(1)}%` : '0%', color: '#22c55e' },
            { label: '盈亏比', value: kpi?.profit_factor?.toFixed(2) || '0', color: '#8b5cf6' },
            { label: '最大回撤', value: kpi ? `$${Math.abs(kpi.max_drawdown).toFixed(0)}` : '$0', color: '#ef4444' },
            { label: '夏普比率', value: kpi?.sharpe_ratio?.toFixed(2) || '0', color: '#3b82f6' },
          ]}
        />

        {/* Block 2 — 交易量 */}
        <KpiCard
          title="交易量"
          value=""
          items={[
            { label: '总信号数', value: kpi?.total_signals?.toLocaleString() || '0', color: '#eab308' },
            { label: '交易次数', value: kpi?.total_trades?.toLocaleString() || '0', color: '#3b82f6' },
            { label: '平均盈利', value: kpi ? `$${kpi.avg_profit.toFixed(0)}` : '$0', color: '#22c55e' },
            { label: '平均亏损', value: kpi ? `$${kpi.avg_loss.toFixed(0)}` : '$0', color: '#ef4444' },
          ]}
        />

        {/* Block 3 — 5 模型运行状态 */}
        <ModelStatusPanel
          inference={latestInference}
          thresholds={scoringThresholds}
        />

        {/* Block 4 — 最近推理 */}
        <KpiCard
          title="最近推理"
          value=""
          items={[
            { label: '分值', value: latestInference ? (latestInference.pre_score || latestInference.score || 0).toFixed(3) : '--', color: '#22c55e' },
            { label: '品种', value: latestInference?.symbol || '--', color: '#3b82f6' },
            { label: '周期', value: latestInference?.timeframe || '--', color: '#8b5cf6' },
            { label: '方向', value: latestInference?.direction || '--', color: latestInference?.direction === 'BUY' ? '#22c55e' : latestInference?.direction === 'SELL' ? '#ef4444' : '#64748b' },
            { label: '信号年龄', value: formatSignalAge(latestInference?.updated_at), color: '#94a3b8', tooltip: latestInference?.signal_id ? `signal_id: ${latestInference.signal_id}` : '' },
          ]}
        />
      </Box>

      {/* ── Main Dashboard: 30% QuoteCard + 70% SignalGaugeChart ── */}
      <Box className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        {/* Left column: Realtime Quote Card */}
        <Box className="xl:col-span-1">
          <RealtimeQuoteCard
            quote={quote}
            loading={quoteLoading}
            error={quoteError}
            symbol={symbol}
            timeframe="M5"
          />
        </Box>

        {/* Right column: Signal Gauge Chart */}
        <Box className="xl:col-span-2">
          <SignalGaugeChart factors={factors} summary={summary} />
        </Box>
      </Box>

      {/* ── Connection/error indicator ── */}
      {(gaugeError || quoteError) && (
        <Typography
          sx={{
            color: '#64748b',
            fontSize: 11,
            mt: 1,
            textAlign: 'center',
          }}
        >
          {gaugeError || quoteError}
        </Typography>
      )}
    </Box>
  );
};

export default Statistics;
