import React, { useEffect, useRef, useState } from 'react';
import { Box, Typography, LinearProgress, Chip } from '@mui/material';
import client from '../../api/client';
import { ENDPOINTS } from '../../api/endpoints';

interface LiveScore {
  symbol?: string;
  time_frame?: string;
  pre_score?: number;
  direction?: string;
  threshold?: number;
  threshold_passed?: boolean;
  adx?: number;
  close?: number;
  ts?: number;
}

const dirColor = (d?: string): string => {
  if (d === 'BUY') return '#22c55e';
  if (d === 'SELL') return '#ef4444';
  return '#94a3b8';
};

const pct = (n?: number): string => ((n ?? 0) * 100).toFixed(1) + '%';

/** 实时评分卡：直观展示"能否下单"——评分 vs 自适应门槛 vs 是否通过。
 *  数据来自信号塔 _live_score_publisher 每 3s 写入 hcm:live:score:{symbol}_{tf}。 */
export default function LiveScoreCard({
  symbol,
  timeframe = 'M5',
}: {
  symbol: string;
  timeframe?: string;
}) {
  const [data, setData] = useState<LiveScore | null>(null);
  const [updatedAt, setUpdatedAt] = useState<string>('--');
  const timer = useRef<number | null>(null);

  useEffect(() => {
    let alive = true;
    const fetchOnce = async () => {
      try {
        const resp = await client.get(ENDPOINTS.dashboard.liveScore, {
          params: { symbol, timeframe },
        });
        const body = resp.data as any;
        const d = body?.data ?? null;
        if (!alive) return;
        setData(d);
        if (d?.ts) {
          setUpdatedAt(new Date(d.ts * 1000).toLocaleTimeString());
        }
      } catch {
        /* 网络抖动忽略 */
      }
    };
    fetchOnce();
    timer.current = window.setInterval(fetchOnce, 2000);
    return () => {
      alive = false;
      if (timer.current) window.clearInterval(timer.current);
    };
  }, [symbol, timeframe]);

  const pre = data?.pre_score ?? 0;
  const thr = data?.threshold ?? 0;
  const passed = data?.threshold_passed ?? false;
  const ratio = thr > 0 ? Math.min(100, (pre / thr) * 100) : 0;

  return (
    <Box
      sx={{
        backgroundColor: '#16161f',
        borderRadius: 2,
        p: 2,
        border: '1px solid #2a2a3a',
      }}
    >
      <Box
        sx={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          mb: 1.5,
        }}
      >
        <Typography variant="subtitle1" sx={{ color: '#e2e8f0', fontWeight: 600 }}>
          实时评分
        </Typography>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <Chip
            label={passed ? (data?.direction ?? 'NO_TRADE') : '不下单'}
            size="small"
            sx={{
              backgroundColor: passed ? dirColor(data?.direction) : '#334155',
              color: passed ? '#0b0b12' : '#cbd5e1',
              fontWeight: 700,
            }}
          />
          {!passed && data?.direction && data.direction !== 'NO_TRADE' && (
            <Typography variant="caption" sx={{ color: '#64748b' }}>
              倾向 {data.direction}（未过门槛）
            </Typography>
          )}
        </Box>
      </Box>

      {data ? (
        <>
          <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1, mb: 0.5 }}>
            <Typography
              variant="h4"
              sx={{ color: passed ? '#22c55e' : '#ef4444', fontWeight: 800 }}
            >
              {pct(pre)}
            </Typography>
            <Typography variant="body2" sx={{ color: '#94a3b8' }}>
              评分 / 门槛 {pct(thr)}
            </Typography>
          </Box>

          {/* 进度条：评分相对门槛的占比 */}
          <LinearProgress
            variant="determinate"
            value={ratio}
            sx={{
              height: 10,
              borderRadius: 5,
              mb: 1.5,
              backgroundColor: '#2a2a3a',
              '& .MuiLinearProgress-bar': {
                backgroundColor: passed ? '#22c55e' : '#eab308',
              },
            }}
          />

          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1.5, alignItems: 'center' }}>
            <Typography variant="caption" sx={{ color: '#94a3b8' }}>
              ADX {data?.adx != null ? data.adx.toFixed(1) : '--'}
            </Typography>
            <Typography variant="caption" sx={{ color: '#64748b' }}>
              更新 {updatedAt}
            </Typography>
          </Box>
        </>
      ) : (
        <Typography variant="body2" sx={{ color: '#64748b' }}>
          等待信号塔实时评分快照…
        </Typography>
      )}
    </Box>
  );
}
