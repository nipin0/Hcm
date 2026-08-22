/** G 区 · 信号日志 —— 前端环形缓冲（v1 零后端改动），刷新页面即清空 */
import React from 'react';
import { Box, Typography, Tooltip, ToggleButton, ToggleButtonGroup } from '@mui/material';
import { C, dirColor, gradeColor, fmt, fmtSigned, tsToClock, LOG_CAPACITY } from './types';
import type { LogEntry, EntryModeKey } from './types';

interface Props {
  logs: LogEntry[];
  /** true=仅记录变化帧；false=记录每一帧 */
  onlyChanges: boolean;
  onToggleMode: (onlyChanges: boolean) => void;
  onClear: () => void;
}

const MODE_SHORT: Record<EntryModeKey, string> = {
  A_PULLBACK: 'A 回踩',
  B_BREAKOUT: 'B 突破',
  WAIT: '观望',
};

const MODE_COLOR: Record<EntryModeKey, string> = {
  A_PULLBACK: C.warn,
  B_BREAKOUT: C.info,
  WAIT: C.textFaint,
};

const HEAD = [
  { label: '时间', w: 74 },
  { label: '方向', w: 62 },
  { label: '等级', w: 46 },
  { label: 'HP', w: 54 },
  { label: 'k', w: 50 },
  { label: '总分', w: 54 },
  { label: '裁决', w: 60 },
  { label: '闸门', w: 54 },
  { label: '模式', w: 62 },
  { label: '变化 / 引擎理由', w: 0 },
];

const LogPanel: React.FC<Props> = ({ logs, onlyChanges, onToggleMode, onClear }) => (
  <Box
    sx={{
      p: 2, borderRadius: 2, background: C.panelBg,
      border: `1px solid ${C.border}`, display: 'flex', flexDirection: 'column',
    }}
  >
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 1, flexWrap: 'wrap' }}>
      <Typography sx={{ fontSize: 13, fontWeight: 700, color: C.textMain }}>
        信号日志
      </Typography>
      <Typography sx={{ fontSize: 10.5, color: C.textFaint }}>
        前端环形缓冲 · 最多 {LOG_CAPACITY} 条 · 刷新页面清空
      </Typography>

      <Box sx={{ flex: 1 }} />

      <ToggleButtonGroup
        size="small"
        exclusive
        value={onlyChanges ? 'change' : 'all'}
        onChange={(_, v) => { if (v) onToggleMode(v === 'change'); }}
        sx={{
          '& .MuiToggleButton-root': {
            fontSize: 10.5, py: 0.2, px: 1, color: C.textDim,
            borderColor: C.border, textTransform: 'none',
          },
          '& .Mui-selected': { color: `${C.info} !important`, background: `${C.info}18 !important` },
        }}
      >
        <ToggleButton value="change">仅记录变化</ToggleButton>
        <ToggleButton value="all">记录每一帧</ToggleButton>
      </ToggleButtonGroup>

      <Box
        component="button"
        onClick={onClear}
        sx={{
          fontSize: 10.5, py: 0.35, px: 1, borderRadius: 1, cursor: 'pointer',
          color: C.textDim, background: 'transparent', border: `1px solid ${C.border}`,
          '&:hover': { color: C.up, borderColor: `${C.up}66` },
        }}
      >
        清空
      </Box>
    </Box>

    {/* 表头 */}
    <Box
      sx={{
        display: 'flex', gap: 1, px: 1, py: 0.6,
        borderBottom: `1px solid ${C.borderStrong}`,
        position: 'sticky', top: 0, background: C.panelBg, zIndex: 1,
      }}
    >
      {HEAD.map((h) => (
        <Typography
          key={h.label}
          sx={{
            fontSize: 10, color: C.textFaint, letterSpacing: '.04em',
            width: h.w || undefined, flex: h.w ? undefined : 1, flexShrink: 0,
          }}
        >
          {h.label}
        </Typography>
      ))}
    </Box>

    {/* 行 */}
    <Box sx={{ maxHeight: 300, overflowY: 'auto' }}>
      {logs.length === 0 && (
        <Typography sx={{ fontSize: 11.5, color: C.textFaint, py: 3, textAlign: 'center' }}>
          暂无记录 —— 等待引擎快照。若长时间为空，请确认信号模式已切换为「和乘幂」。
        </Typography>
      )}
      {logs.map((e) => {
        const dc = dirColor(e.direction);
        const gc = gradeColor(e.grade);
        return (
          <Box
            key={e.id}
            sx={{
              display: 'flex', gap: 1, px: 1, py: 0.5, alignItems: 'center',
              borderBottom: `1px solid rgba(148,163,184,0.07)`,
              '&:hover': { background: 'rgba(148,163,184,0.05)' },
            }}
          >
            <Typography sx={{ width: 74, flexShrink: 0, fontSize: 11, color: C.textDim, fontVariantNumeric: 'tabular-nums' }}>
              {tsToClock(e.ts)}
            </Typography>
            <Typography sx={{ width: 62, flexShrink: 0, fontSize: 11, fontWeight: 700, color: dc }}>
              {e.direction === 'NO_TRADE' ? '—' : e.direction}
            </Typography>
            <Box sx={{ width: 46, flexShrink: 0 }}>
              <Box
                component="span"
                sx={{
                  fontSize: 10, fontWeight: 700, color: gc,
                  border: `1px solid ${gc}66`, borderRadius: 0.6, px: 0.5, py: 0.05,
                }}
              >
                {e.grade}
              </Box>
            </Box>
            <Typography sx={{ width: 54, flexShrink: 0, fontSize: 11, color: C.textMain, fontVariantNumeric: 'tabular-nums' }}>
              {fmt(e.hpScore, 1)}
            </Typography>
            <Typography sx={{ width: 50, flexShrink: 0, fontSize: 11, color: C.violet, fontVariantNumeric: 'tabular-nums' }}>
              {fmt(e.k, 2)}
            </Typography>
            <Typography sx={{ width: 54, flexShrink: 0, fontSize: 11, color: C.textMain, fontVariantNumeric: 'tabular-nums' }}>
              {fmt(e.total, 1)}
            </Typography>
            <Typography
              sx={{
                width: 60, flexShrink: 0, fontSize: 11, fontVariantNumeric: 'tabular-nums',
                color: e.verdict > 0 ? C.up : e.verdict < 0 ? C.down : C.textDim,
              }}
            >
              {fmtSigned(e.verdict, 2)}
            </Typography>
            <Typography
              sx={{ width: 54, flexShrink: 0, fontSize: 11, fontWeight: 700, color: e.passed ? C.up : C.textFaint }}
            >
              {e.passed ? '✓放行' : '✗拦截'}
            </Typography>
            <Typography sx={{ width: 62, flexShrink: 0, fontSize: 10.5, color: MODE_COLOR[e.entryMode] }}>
              {MODE_SHORT[e.entryMode]}
            </Typography>
            <Tooltip title={e.reason} arrow>
              <Typography
                sx={{
                  flex: 1, fontSize: 10.5, color: C.textDim,
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}
              >
                {e.change}
                <span style={{ color: C.textFaint }}> · {e.reason}</span>
              </Typography>
            </Tooltip>
          </Box>
        );
      })}
    </Box>
  </Box>
);

export default LogPanel;
