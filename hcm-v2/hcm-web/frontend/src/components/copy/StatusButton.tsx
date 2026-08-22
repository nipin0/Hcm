import React from 'react';
import { IconButton, Tooltip, CircularProgress } from '@mui/material';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import PauseIcon from '@mui/icons-material/Pause';
import StopIcon from '@mui/icons-material/Stop';
import type { CopyStatus } from '../../types/copy';

interface StatusButtonProps {
  status: CopyStatus;
  loading?: boolean;
  onChange: (newStatus: CopyStatus) => void;
}

/** Status-to-next mapping: clicking a status triggers the next logical state. */
const STATUS_NEXT: Record<CopyStatus, CopyStatus | null> = {
  stopped: 'running',
  running: 'paused',
  paused: 'stopped',
};

const STATUS_COLOR: Record<CopyStatus, string> = {
  running: '#22c55e',
  stopped: '#ef4444',
  paused: '#eab308',
};

const STATUS_LABEL: Record<CopyStatus, string> = {
  stopped: '已停止 — 点击启动',
  running: '运行中 — 点击暂停',
  paused: '已暂停 — 点击停止',
};

const StatusButton: React.FC<StatusButtonProps> = ({ status, loading, onChange }) => {
  const next = STATUS_NEXT[status];

  const renderIcon = (): React.ReactNode => {
    if (loading) {
      return <CircularProgress size={20} sx={{ color: STATUS_COLOR[status] }} />;
    }

    switch (status) {
      case 'running':
        return <PauseIcon fontSize="small" />;
      case 'stopped':
        return <PlayArrowIcon fontSize="small" />;
      case 'paused':
        return <StopIcon fontSize="small" />;
      default:
        return <PlayArrowIcon fontSize="small" />;
    }
  };

  const handleClick = (): void => {
    if (loading || next === null) return;
    onChange(next);
  };

  return (
    <Tooltip title={STATUS_LABEL[status]}>
      <IconButton
        size="small"
        onClick={handleClick}
        disabled={loading}
        sx={{
          color: STATUS_COLOR[status],
          '&:hover': {
            backgroundColor: `${STATUS_COLOR[status]}22`,
          },
        }}
      >
        {renderIcon()}
      </IconButton>
    </Tooltip>
  );
};

export default StatusButton;
export { STATUS_COLOR };
