import React, { useState, useEffect, useCallback } from 'react';
import { Box, Typography, Chip, LinearProgress, Button } from '@mui/material';
import { RefreshCw, Pause, PlayArrow, RestartAlt } from '../../components/Icons';
import client from '../../api/client';

interface WatchdogInfo {
  status: string;
  uptime_seconds: number;
  last_check: string;
  watched_services: { name: string; status: string; last_heartbeat: string }[];
  restarts: number;
  alerts: { level: string; message: string; time: string }[];
}

const Watchdog: React.FC = () => {
  const [info, setInfo] = useState<WatchdogInfo | null>(null);
  const [loading, setLoading] = useState<boolean>(false);

  const fetchStatus = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const { data } = await client.get('/api/signal-tower/watchdog');
      setInfo(data.data || data);
    } catch {
      setInfo(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchStatus();
    const interval = setInterval(fetchStatus, 10000);
    return () => clearInterval(interval);
  }, [fetchStatus]);

  const handleAction = async (action: string): Promise<void> => {
    try {
      await client.post(`/api/signal-tower/watchdog/${action}`);
      fetchStatus();
    } catch {
      // Handle error
    }
  };

  const formatUptime = (seconds: number): string => {
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    const parts: string[] = [];
    if (d > 0) parts.push(`${d}d`);
    if (h > 0) parts.push(`${h}h`);
    if (m > 0) parts.push(`${m}m`);
    parts.push(`${s}s`);
    return parts.join(' ');
  };

  const alertColor = (level: string): 'error' | 'warning' | 'info' => {
    switch (level) {
      case 'critical': return 'error';
      case 'warning': return 'warning';
      default: return 'info';
    }
  };

  return (
    <Box>
      <Box className="flex items-center justify-between mb-6">
        <Typography variant="h6" className="text-gray-100 font-semibold">看门狗状态</Typography>
        <Box className="flex gap-2">
          <Button variant="outlined" startIcon={<Pause />} onClick={() => handleAction('pause')}
            sx={{ borderColor: '#4b5563', color: '#94a3b8' }}>暂停</Button>
          <Button variant="outlined" startIcon={<PlayArrow />} onClick={() => handleAction('resume')}
            sx={{ borderColor: '#4b5563', color: '#94a3b8' }}>恢复</Button>
          <Button variant="outlined" startIcon={<RestartAlt />} onClick={() => handleAction('restart')}
            sx={{ borderColor: '#4b5563', color: '#94a3b8' }}>重启</Button>
          <Button variant="outlined" startIcon={<RefreshCw />} onClick={fetchStatus} disabled={loading}
            sx={{ borderColor: '#4b5563', color: '#94a3b8' }}>刷新</Button>
        </Box>
      </Box>

      {loading && !info ? (
        <LinearProgress sx={{ backgroundColor: '#1a1a24', '& .MuiLinearProgress-bar': { backgroundColor: '#3b82f6' } }} />
      ) : info ? (
        <Box>
          {/* Status Header */}
          <Box className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
            <Box className="card">
              <Typography variant="caption" className="text-gray-400 uppercase tracking-wider">守护进程状态</Typography>
              <Box className="flex items-center gap-2 mt-2">
                <Chip
                  label={info.status === 'running' ? '运行中' : info.status}
                  size="medium"
                  color={info.status === 'running' ? 'success' : 'error'}
                />
              </Box>
            </Box>
            <Box className="card">
              <Typography variant="caption" className="text-gray-400 uppercase tracking-wider">运行时间</Typography>
              <Typography className="text-2xl font-bold text-gray-100 mt-1">
                {formatUptime(info.uptime_seconds)}
              </Typography>
            </Box>
            <Box className="card">
              <Typography variant="caption" className="text-gray-400 uppercase tracking-wider">重启次数</Typography>
              <Typography className="text-2xl font-bold text-gray-100 mt-1">{info.restarts}</Typography>
            </Box>
            <Box className="card">
              <Typography variant="caption" className="text-gray-400 uppercase tracking-wider">最后检查</Typography>
              <Typography className="text-lg font-semibold text-gray-200 mt-1">{info.last_check}</Typography>
            </Box>
          </Box>

          {/* Watched Services */}
          <Box className="card mb-6">
            <Typography className="text-sm font-medium text-gray-300 mb-3">受监控服务</Typography>
            <Box className="space-y-2">
              {info.watched_services?.map((svc) => (
                <Box key={svc.name} className="flex items-center justify-between bg-gray-800/50 rounded-lg px-4 py-2.5">
                  <Typography className="text-sm text-gray-300">{svc.name}</Typography>
                  <Box className="flex items-center gap-3">
                    <Typography variant="caption" className="text-gray-500">{svc.last_heartbeat}</Typography>
                    <Chip
                      label={svc.status}
                      size="small"
                      color={svc.status === 'healthy' ? 'success' : svc.status === 'degraded' ? 'warning' : 'error'}
                      sx={{ fontSize: 11, height: 22 }}
                    />
                  </Box>
                </Box>
              ))}
            </Box>
          </Box>

          {/* Alerts */}
          {info.alerts?.length > 0 && (
            <Box className="card">
              <Typography className="text-sm font-medium text-gray-300 mb-3">告警记录</Typography>
              <Box className="space-y-1.5">
                {info.alerts.map((alert, idx) => (
                  <Box key={idx} className="flex items-center gap-3 bg-gray-800/30 rounded-lg px-4 py-2">
                    <Chip label={alert.level} size="small" color={alertColor(alert.level)} sx={{ fontSize: 10, height: 20 }} />
                    <Typography className="text-sm text-gray-300 flex-1">{alert.message}</Typography>
                    <Typography variant="caption" className="text-gray-500">{alert.time}</Typography>
                  </Box>
                ))}
              </Box>
            </Box>
          )}
        </Box>
      ) : (
        <Typography className="text-gray-500 text-center py-12">无法获取看门狗状态</Typography>
      )}
    </Box>
  );
};

export default Watchdog;
