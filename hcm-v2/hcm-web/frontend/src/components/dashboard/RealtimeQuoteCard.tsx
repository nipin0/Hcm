import React from 'react';
import { Box, Typography, Paper, Skeleton, Chip } from '@mui/material';
import type { RealtimeQuote } from '../../types/signalGauge';

interface RealtimeQuoteCardProps {
  quote: RealtimeQuote | null;
  loading: boolean;
  error: string | null;
  symbol: string;
  timeframe?: string;
}

/** Format number with locale string and given decimal digits */
function fmt(val: number, digits: number = 2): string {
  return val.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

/** Real-time quote card — left column 30% layout */
const RealtimeQuoteCard: React.FC<RealtimeQuoteCardProps> = ({
  quote,
  loading,
  error,
  symbol,
  timeframe = 'M5',
}) => {
  const isPositive: boolean = (quote?.change ?? 0) >= 0;
  const priceColor: string = isPositive ? '#22c55e' : '#ef4444';

  // Loading skeleton state
  if (loading && !quote) {
    return (
      <Paper
        elevation={0}
        sx={{
          backgroundColor: '#0f0f17',
          border: '1px solid #2a2a3a',
          borderRadius: 3,
          p: 2.5,
          minHeight: 380,
        }}
      >
        <Box className="flex items-center gap-2 mb-3">
          <Skeleton variant="text" width={60} height={24} sx={{ bgcolor: '#1a1a24' }} />
          <Skeleton variant="rounded" width={36} height={20} sx={{ bgcolor: '#1a1a24' }} />
        </Box>
        <Skeleton variant="text" width={140} height={42} sx={{ bgcolor: '#1a1a24' }} />
        <Skeleton variant="text" width={100} height={20} sx={{ bgcolor: '#1a1a24', mt: 1 }} />
        <Box className="mt-4 grid grid-cols-2 gap-3">
          <Skeleton variant="rounded" height={40} sx={{ bgcolor: '#1a1a24' }} />
          <Skeleton variant="rounded" height={40} sx={{ bgcolor: '#1a1a24' }} />
          <Skeleton variant="rounded" height={40} sx={{ bgcolor: '#1a1a24' }} />
          <Skeleton variant="rounded" height={40} sx={{ bgcolor: '#1a1a24' }} />
          <Skeleton variant="rounded" height={40} sx={{ bgcolor: '#1a1a24' }} />
          <Skeleton variant="rounded" height={40} sx={{ bgcolor: '#1a1a24' }} />
        </Box>
      </Paper>
    );
  }

  return (
    <Paper
      elevation={0}
      sx={{
        backgroundColor: '#0f0f17',
        border: '1px solid #2a2a3a',
        borderRadius: 3,
        p: 2.5,
        minHeight: 380,
      }}
    >
      {/* Header: symbol + timeframe badge */}
      <Box className="flex items-center gap-2 mb-1">
        <Typography sx={{ color: '#f1f5f9', fontSize: 16, fontWeight: 700 }}>
          {symbol}
        </Typography>
        <Chip
          label={timeframe}
          size="small"
          sx={{
            backgroundColor: 'rgba(59,130,246,0.15)',
            color: '#3b82f6',
            fontSize: 10,
            height: 20,
            fontWeight: 600,
          }}
        />
      </Box>

      {/* Large price display */}
      <Typography sx={{ color: priceColor, fontSize: 36, fontWeight: 800, lineHeight: 1.2, mb: 0.5 }}>
        {quote ? fmt(quote.last, quote.last >= 100 ? 2 : quote.last >= 1 ? 3 : 5) : '--'}
      </Typography>

      {/* Change row */}
      <Box className="flex items-center gap-3 mb-4">
        <Typography sx={{ color: priceColor, fontSize: 14, fontWeight: 600 }}>
          {isPositive ? '+' : ''}{quote ? fmt(quote.change, 2) : '--'}
        </Typography>
        <Typography sx={{ color: priceColor, fontSize: 14, fontWeight: 600 }}>
          ({isPositive ? '+' : ''}{quote?.change_pct?.toFixed(2) ?? '--'}%)
        </Typography>
      </Box>

      {/* Bid / Ask row */}
      <Box className="grid grid-cols-2 gap-3 mb-3">
        <Box
          sx={{
            backgroundColor: 'rgba(239,68,68,0.08)',
            border: '1px solid rgba(239,68,68,0.2)',
            borderRadius: 2,
            p: 1.25,
          }}
        >
          <Typography sx={{ color: '#64748b', fontSize: 10, textTransform: 'uppercase', mb: 0.5 }}>
            Bid
          </Typography>
          <Typography sx={{ color: '#ef4444', fontSize: 15, fontWeight: 700 }}>
            {quote ? fmt(quote.bid, quote.bid >= 100 ? 2 : 3) : '--'}
          </Typography>
        </Box>
        <Box
          sx={{
            backgroundColor: 'rgba(34,197,94,0.08)',
            border: '1px solid rgba(34,197,94,0.2)',
            borderRadius: 2,
            p: 1.25,
          }}
        >
          <Typography sx={{ color: '#64748b', fontSize: 10, textTransform: 'uppercase', mb: 0.5 }}>
            Ask
          </Typography>
          <Typography sx={{ color: '#22c55e', fontSize: 15, fontWeight: 700 }}>
            {quote ? fmt(quote.ask, quote.ask >= 100 ? 2 : 3) : '--'}
          </Typography>
        </Box>
      </Box>

      {/* O/H/L and volume */}
      <Box className="grid grid-cols-2 gap-3 mb-3">
        <DataCell label="开盘" value={quote?.open} />
        <DataCell label="最高" value={quote?.high} color="#22c55e" />
        <DataCell label="最低" value={quote?.low} color="#ef4444" />
        <DataCell label="成交量" value={quote?.volume} isInt />
      </Box>

      {/* Updated timestamp */}
      <Typography sx={{ color: '#475569', fontSize: 10 }}>
        {quote?.updated_at
          ? `更新于 ${new Date(
              typeof quote.updated_at === 'number' ? quote.updated_at * 1000 : quote.updated_at,
            ).toLocaleTimeString('zh-CN', { hour12: false })}`
          : '--'}
      </Typography>

      {/* Error indicator */}
      {error && (
        <Typography sx={{ color: '#64748b', fontSize: 10, mt: 0.5 }}>
          数据更新失败
        </Typography>
      )}
    </Paper>
  );
};

/** Small data cell for O/H/L/Volume */
const DataCell: React.FC<{
  label: string;
  value: number | undefined;
  color?: string;
  isInt?: boolean;
}> = ({ label, value, color = '#94a3b8', isInt = false }) => (
  <Box>
    <Typography sx={{ color: '#64748b', fontSize: 10, textTransform: 'uppercase' }}>
      {label}
    </Typography>
    <Typography sx={{ color, fontSize: 14, fontWeight: 600 }}>
      {value !== undefined
        ? isInt
          ? value.toLocaleString()
          : value >= 100
            ? value.toFixed(2)
            : value.toFixed(3)
        : '--'}
    </Typography>
  </Box>
);

export default RealtimeQuoteCard;
