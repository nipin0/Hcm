import React from 'react';
import { Box, Typography } from '@mui/material';
import type { SignalGaugeSummary } from '../../types/signalGauge';

interface SignalSummaryBarProps {
  summary: SignalGaugeSummary;
  variant: 'top' | 'bottom';
}

/** Signal summary bar — top variant shows colored badges, bottom shows text legend */
const SignalSummaryBar: React.FC<SignalSummaryBarProps> = ({ summary, variant }) => {
  if (variant === 'top') {
    return (
      <Box className="flex items-center gap-3">
        {/* Long badge */}
        <Box
          sx={{
            backgroundColor: 'rgba(34,197,94,0.15)',
            border: '1px solid rgba(34,197,94,0.3)',
            borderRadius: 1.5,
            px: 1.5,
            py: 0.5,
          }}
        >
          <Typography sx={{ color: '#22c55e', fontSize: 12, fontWeight: 700 }}>
            ▲多 {summary.long}
          </Typography>
        </Box>
        {/* Short badge */}
        <Box
          sx={{
            backgroundColor: 'rgba(239,68,68,0.15)',
            border: '1px solid rgba(239,68,68,0.3)',
            borderRadius: 1.5,
            px: 1.5,
            py: 0.5,
          }}
        >
          <Typography sx={{ color: '#ef4444', fontSize: 12, fontWeight: 700 }}>
            空 {summary.short}
          </Typography>
        </Box>
      </Box>
    );
  }

  // variant === 'bottom': text legend row
  return (
    <Box className="flex items-center justify-center gap-6 py-2">
      <Typography sx={{ color: '#22c55e', fontSize: 12, fontWeight: 600 }}>
        ▲ 多平 {summary.long}
      </Typography>
      <Typography sx={{ color: '#94a3b8', fontSize: 12, fontWeight: 500 }}>
        - 中性 {summary.neutral} -
      </Typography>
      <Typography sx={{ color: '#ef4444', fontSize: 12, fontWeight: 600 }}>
        ▼ 空平 {summary.short}
      </Typography>
    </Box>
  );
};

export default SignalSummaryBar;
