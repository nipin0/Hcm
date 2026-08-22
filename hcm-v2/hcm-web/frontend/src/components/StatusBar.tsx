import React, { useState, useEffect } from 'react';
import { Box, Typography, Tooltip } from '@mui/material';
import {
  Storage,
  Memory,
  Hub,
  FiberManualRecord,
  Wifi,
} from "./Icons";
import client from '../api/client';

interface ServiceStatus {
  name: string;
  status: 'online' | 'offline' | 'degraded';
  latency?: number;
}

const StatusBar: React.FC = () => {
  const [services, setServices] = useState<ServiceStatus[]>([
    { name: 'PG', status: 'offline' },
    { name: 'Redis', status: 'offline' },
    { name: 'Gateway', status: 'offline' },
  ]);
  const [wsConnected, setWsConnected] = useState<boolean>(false);
  const [currentTime, setCurrentTime] = useState<string>('');

  // Update clock
  useEffect(() => {
    const updateClock = (): void => {
      setCurrentTime(new Date().toLocaleString('zh-CN'));
    };
    updateClock();
    const timer = setInterval(updateClock, 1000);
    return () => clearInterval(timer);
  }, []);

  // Poll health status
  useEffect(() => {
    const fetchHealth = async (): Promise<void> => {
      try {
        const { data: resp } = await client.get('/api/v1/health');
        const d = resp.data || resp;  // unwrap {code, data, message}
        const components = d.components || {};
        setServices([
          { name: 'PG', status: components.postgresql?.status === 'healthy' ? 'online' : 'offline',
            latency: components.postgresql?.latency_ms },
          { name: 'Redis', status: components.redis?.status === 'healthy' ? 'online' : 'offline',
            latency: components.redis?.latency_ms },
          { name: 'Gateway', status: components.self?.status === 'healthy' ? 'online' : 'offline',
            latency: undefined },
        ]);
        setWsConnected(d.status === 'healthy');  // healthy → WS online
      } catch {
        setServices((prev) => prev.map((s) => ({ ...s, status: 'offline' as const })));
        setWsConnected(false);
      }
    };

    fetchHealth();
    const interval = setInterval(fetchHealth, 15000);
    return () => clearInterval(interval);
  }, []);

  const statusColor = (status: string): string => {
    switch (status) {
      case 'online': return '#22c55e';
      case 'degraded': return '#eab308';
      case 'offline': return '#ef4444';
      default: return '#64748b';
    }
  };

  const serviceIcon = (name: string): React.ReactNode => {
    switch (name) {
      case 'PG': return <Storage sx={{ fontSize: 14 }} />;
      case 'Redis': return <Memory sx={{ fontSize: 14 }} />;
      case 'Gateway': return <Hub sx={{ fontSize: 14 }} />;
      default: return null;
    }
  };

  return (
    <Box
      className="h-8 bg-gray-950 border-t border-gray-800 flex items-center justify-between px-4 shrink-0"
    >
      {/* Left: Service Status */}
      <Box className="flex items-center gap-4">
        {services.map((svc) => (
          <Tooltip
            key={svc.name}
            title={`${svc.name}: ${svc.status}${svc.latency !== undefined ? ` (${svc.latency}ms)` : ''}`}
            arrow
          >
            <Box className="flex items-center gap-1.5 cursor-default">
              {serviceIcon(svc.name)}
              <Typography variant="caption" sx={{ color: '#94a3b8', fontSize: 11 }}>
                {svc.name}
              </Typography>
              <FiberManualRecord
                sx={{
                  fontSize: 8,
                  color: statusColor(svc.status),
                }}
              />
            </Box>
          </Tooltip>
        ))}

        <Box className="w-px h-4 bg-gray-700" />

        <Tooltip title={`WebSocket: ${wsConnected ? '已连接' : '未连接'}`} arrow>
          <Box className="flex items-center gap-1.5 cursor-default">
            <Wifi sx={{ fontSize: 14, color: wsConnected ? '#22c55e' : '#64748b' }} />
            <Typography variant="caption" sx={{ color: '#94a3b8', fontSize: 11 }}>
              WS
            </Typography>
            <FiberManualRecord
              sx={{
                fontSize: 8,
                color: wsConnected ? '#22c55e' : '#64748b',
              }}
            />
          </Box>
        </Tooltip>
      </Box>

      {/* Right: Clock */}
      <Typography variant="caption" sx={{ color: '#64748b', fontSize: 11 }}>
        {currentTime}
      </Typography>
    </Box>
  );
};

export default StatusBar;
