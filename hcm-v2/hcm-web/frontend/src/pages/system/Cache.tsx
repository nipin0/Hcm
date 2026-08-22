import React, { useEffect, useCallback, useState } from 'react';
import { Box, Typography, Button, LinearProgress, Chip } from '@mui/material';
import { RefreshCw, DeleteSweep } from '../../components/Icons';
import client from '../../api/client';
import { ENDPOINTS } from '../../api/endpoints';

interface CacheStats {
  redis_connected: boolean;
  redis_version: string;
  hit_rate: number;        // 已是百分比 (e.g. 99.99)，不是 0-1 小数
  size_mb: number;
  total_keys: number;
  ttl_seconds: number;
  uptime_seconds: number;
}

const Cache: React.FC = () => {
  const [stats, setStats] = useState<CacheStats | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [clearing, setClearing] = useState<boolean>(false);

  const fetchStats = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const { data } = await client.get(ENDPOINTS.system.cacheStats);
      setStats(data.data || data);
    } catch {
      setStats(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchStats();
  }, [fetchStats]);

  const handleClear = async (): Promise<void> => {
    if (!window.confirm('确认清除所有缓存？此操作不可撤销。')) return;
    setClearing(true);
    try {
      await client.post(ENDPOINTS.system.cacheClear);
      fetchStats();
    } catch {
      // Handle error
    } finally {
      setClearing(false);
    }
  };

  return (
    <Box>
      <Box className="flex items-center justify-between mb-6">
        <Typography variant="h6" className="text-gray-100 font-semibold">
          缓存管理
        </Typography>
        <Box className="flex gap-2">
          <Button
            variant="outlined"
            startIcon={<RefreshCw />}
            onClick={fetchStats}
            disabled={loading}
            sx={{ borderColor: '#4b5563', color: '#94a3b8' }}
          >
            刷新
          </Button>
          <Button
            variant="contained"
            color="error"
            startIcon={<DeleteSweep />}
            onClick={handleClear}
            disabled={clearing}
            sx={{ '&.MuiButton-containedError': { backgroundColor: '#dc2626' } }}
          >
            清除缓存
          </Button>
        </Box>
      </Box>

      {loading && !stats ? (
        <LinearProgress sx={{ backgroundColor: '#1a1a24', '& .MuiLinearProgress-bar': { backgroundColor: '#3b82f6' } }} />
      ) : stats ? (
        <Box className="grid grid-cols-1 md:grid-cols-4 gap-4">
          <Box className="card">
            <Typography variant="caption" className="text-gray-400 uppercase tracking-wider">Redis 版本</Typography>
            <Typography className="text-2xl font-bold text-gray-100 mt-1">{stats.redis_version}</Typography>
            <Chip
              label={stats.redis_connected ? '已连接' : '断开'}
              size="small"
              color={stats.redis_connected ? 'success' : 'warning'}
              sx={{ mt: 1 }}
            />
          </Box>

          <Box className="card">
            <Typography variant="caption" className="text-gray-400 uppercase tracking-wider">缓存大小</Typography>
            <Typography className="text-2xl font-bold text-gray-100 mt-1">{stats.size_mb.toFixed(1)} MB</Typography>
            <Typography variant="caption" className="text-gray-500 mt-1 block">
              {stats.total_keys.toLocaleString()} 个键
            </Typography>
          </Box>

          <Box className="card">
            <Typography variant="caption" className="text-gray-400 uppercase tracking-wider">命中率</Typography>
            <Typography className="text-2xl font-bold text-green-400 mt-1">{stats.hit_rate.toFixed(1)}%</Typography>
            <Box className="mt-2">
              <LinearProgress
                variant="determinate"
                value={stats.hit_rate}
                sx={{
                  height: 6,
                  borderRadius: 3,
                  backgroundColor: '#1a1a24',
                  '& .MuiLinearProgress-bar': { backgroundColor: '#22c55e', borderRadius: 3 },
                }}
              />
            </Box>
          </Box>

          <Box className="card">
            <Typography variant="caption" className="text-gray-400 uppercase tracking-wider">运行时长</Typography>
            <Typography className="text-2xl font-bold text-gray-100 mt-1">
              {Math.floor(stats.uptime_seconds / 3600)}h {Math.floor((stats.uptime_seconds % 3600) / 60)}m
            </Typography>
            <Typography variant="caption" className="text-gray-500 mt-1 block">
              TTL: {stats.ttl_seconds}s
            </Typography>
          </Box>
        </Box>
      ) : (
        <Typography className="text-gray-500 text-center py-12">暂无缓存数据</Typography>
      )}
    </Box>
  );
};

export default Cache;
