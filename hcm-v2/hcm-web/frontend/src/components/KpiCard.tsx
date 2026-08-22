import React from 'react';
import { Box, Typography, Paper, Tooltip } from '@mui/material';
import { TrendingUp, TrendingDown } from './Icons';

/** A single sub-item displayed below the divider */
interface KpiSubItem {
  label: string;
  value: string;
  color?: string;
  /** Tooltip text shown on hover; omit to disable */
  tooltip?: string;
}

interface KpiCardProps {
  title: string;
  /** items 模式下不展示单值，故可选；标准单值模式仍应传 */
  value?: string | number;
  unit?: string;
  change?: number;
  changeLabel?: string;
  icon?: React.ReactNode;
  color?: string;
  subtitle?: string;
  /** Optional bottom key-value row(s) shown below a divider line */
  sub?: KpiSubItem;
  /** Multi-row items mode — when set, renders items list instead of value/icon */
  items?: KpiSubItem[];
}

const KpiCard: React.FC<KpiCardProps> = ({
  title,
  value,
  unit,
  change,
  changeLabel,
  icon,
  color = '#3b82f6',
  subtitle,
  sub,
  items,
}) => {
  const isPositive: boolean = (change ?? 0) >= 0;

  /* ── Items mode: render title + multi-row list ── */
  if (items && items.length > 0) {
    return (
      <Paper
        elevation={0}
        sx={{
          backgroundColor: '#111118',
          border: '1px solid #2a2a3a',
          borderRadius: 3,
          p: 2.5,
          minWidth: 180,
          position: 'relative',
          overflow: 'hidden',
          display: 'flex',
          flexDirection: 'column',
          '&::before': {
            content: '""',
            position: 'absolute',
            top: 0,
            left: 0,
            right: 0,
            height: 3,
            backgroundColor: color,
            borderTopLeftRadius: 3,
            borderTopRightRadius: 3,
          },
        }}
      >
        {/* Title */}
        <Typography
          variant="caption"
          sx={{
            color: '#94a3b8',
            textTransform: 'uppercase',
            letterSpacing: '0.05em',
            fontWeight: 500,
            mb: 1,
          }}
        >
          {title}
        </Typography>

        {/* Items list — flex column with space-between for even vertical distribution */}
        <Box
          sx={{
            display: 'flex',
            flexDirection: 'column',
            justifyContent: 'space-between',
            flex: 1,
          }}
        >
          {items.map((item, i) => (
            <Tooltip
              key={i}
              title={item.tooltip || ''}
              arrow
              placement="top"
              disableHoverListener={!item.tooltip}
            >
              <Box
                sx={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  py: 0.6,
                  borderBottom:
                    i < items.length - 1 ? '1px solid #1e293b' : 'none',
                  cursor: item.tooltip ? 'help' : 'default',
                }}
              >
                <Typography variant="caption" sx={{ color: '#94a3b8' }}>
                  {item.label}
                </Typography>
                <Typography
                  variant="body2"
                  sx={{ color: item.color || '#e2e8f0', fontWeight: 600 }}
                >
                  {item.value}
                </Typography>
              </Box>
            </Tooltip>
          ))}
        </Box>
      </Paper>
    );
  }

  /* ── Standard single-value mode ── */
  return (
    <Paper
      elevation={0}
      sx={{
        backgroundColor: '#111118',
        border: '1px solid #2a2a3a',
        borderRadius: 3,
        p: 2.5,
        minWidth: 180,
        position: 'relative',
        overflow: 'hidden',
        '&::before': {
          content: '""',
          position: 'absolute',
          top: 0,
          left: 0,
          right: 0,
          height: 3,
          backgroundColor: color,
          borderTopLeftRadius: 3,
          borderTopRightRadius: 3,
        },
      }}
    >
      <Box className="flex items-center justify-between mb-2">
        <Typography
          variant="caption"
          className="text-gray-400 uppercase tracking-wider font-medium"
        >
          {title}
        </Typography>
        {icon && <Box sx={{ color, opacity: 0.8 }}>{icon}</Box>}
      </Box>

      <Box className="flex items-baseline gap-1 mb-1">
        <Typography
          sx={{ fontSize: 28, fontWeight: 700, color: '#f1f5f9', lineHeight: 1.2 }}
        >
          {value}
        </Typography>
        {unit && (
          <Typography variant="caption" className="text-gray-500">
            {unit}
          </Typography>
        )}
      </Box>

      {subtitle && (
        <Typography variant="caption" className="text-gray-500 block mb-1">
          {subtitle}
        </Typography>
      )}

      {change !== undefined && (
        <Box className="flex items-center gap-1">
          {isPositive ? (
            <TrendingUp sx={{ fontSize: 14, color: '#22c55e' }} />
          ) : (
            <TrendingDown sx={{ fontSize: 14, color: '#ef4444' }} />
          )}
          <Typography
            variant="caption"
            sx={{ fontWeight: 600, color: isPositive ? '#22c55e' : '#ef4444' }}
          >
            {isPositive ? '+' : ''}
            {change.toFixed(2)}%
          </Typography>
          {changeLabel && (
            <Typography variant="caption" className="text-gray-500">
              {changeLabel}
            </Typography>
          )}
        </Box>
      )}

      {sub && (
        <>
          <Box sx={{ borderTop: '1px solid #2a2a3a', my: 1.5 }} />
          <Box className="flex items-center justify-between">
            <Typography variant="caption" sx={{ color: '#64748b' }}>
              {sub.label}
            </Typography>
            <Typography
              variant="caption"
              sx={{ color: sub.color || '#e2e8f0', fontWeight: 600 }}
            >
              {sub.value}
            </Typography>
          </Box>
        </>
      )}
    </Paper>
  );
};

export default KpiCard;
