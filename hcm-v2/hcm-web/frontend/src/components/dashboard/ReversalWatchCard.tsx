import React, { useMemo } from 'react';
import { Box, Typography, Chip, Tooltip, Paper } from '@mui/material';
import { useSymbol } from '../../contexts/SymbolContext';
import { useSignalGauges } from '../../api/signalGauge';

/**
 * 抄底 / 抄顶监控卡
 * 放置在「实时信号流」面板顶部，实时反映行情是否进入双源信号的
 * 均值回归（抄底 / 抄顶）窗口。
 *
 * 双源(co_source)抄底逻辑仅在「震荡市(RANGE / NEUTRAL) + RSI 极值」
 * 时放行 BUY/SELL，趋势市(TREND)下的逆势单会被 H1 防火墙 + calib 拦截。
 * 本卡把这套「窗口是否开启」实时可视化，行情转 RANGE 且 RSI 进入极值带时
 * 给出醒目提示（此时系统会自动出单抄底/抄顶）。
 */

// 阈值与后台双源抄底配置对齐（scoring.range_rsi_extreme_low / _high）
const RSI_OVERSOLD = 35; // RSI ≤ 该值 → 抄底(BUY)窗口
const RSI_OVERBOUGHT = 65; // RSI ≥ 该值 → 抄顶(SELL)窗口
const ADX_STRONG = 24; // 强趋势门槛（thresholds.strong_adx_threshold）

type Tone = 'ok' | 'warn' | 'block' | 'idle' | 'info';
const BG: Record<Tone, string> = {
  ok: 'rgba(34,197,94,0.15)',
  warn: 'rgba(234,179,8,0.15)',
  block: 'rgba(239,68,68,0.15)',
  idle: 'rgba(100,116,139,0.15)',
  info: 'rgba(59,130,246,0.15)',
};
const FG: Record<Tone, string> = {
  ok: '#22c55e',
  warn: '#eab308',
  block: '#ef4444',
  idle: '#94a3b8',
  info: '#3b82f6',
};

/** 从 factor gauges 的 rsi 因子 raw_display（如 "RSI28"）解析真实 RSI 值 */
function parseRsi(rawDisplay?: string): number | null {
  if (!rawDisplay) return null;
  const m = rawDisplay.match(/RSI\s*([\d.]+)/i);
  return m ? parseFloat(m[1]) : null;
}

function regimeTone(regime: string): Tone {
  if (regime === 'RANGE') return 'warn';
  if (regime === 'TREND') return 'info';
  if (regime === 'TREND_FADE') return 'info';
  return 'idle';
}

function regimeLabel(regime: string): string {
  switch (regime) {
    case 'RANGE':
      return '震荡市';
    case 'TREND':
      return '趋势市';
    case 'TREND_FADE':
      return '趋势减弱';
    case 'NEUTRAL':
      return '中性';
    default:
      return regime || '中性';
  }
}

const ReversalWatchCard: React.FC = () => {
  const { selectedSymbol } = useSymbol();
  const symbol = selectedSymbol?.symbol || 'XAUUSD';
  const { factors, latestInference, scoringThresholds } = useSignalGauges(symbol);

  const rsi = useMemo<number | null>(() => {
    const f = factors.find((x) => x.key === 'rsi');
    return parseRsi(f?.raw_display);
  }, [factors]);

  const adx = latestInference?.live_adx_14 || latestInference?.adx_14 || 0;
  const regime = latestInference?.regime || 'NEUTRAL';
  const strongAdx = scoringThresholds?.strong_adx_threshold || ADX_STRONG;

  // 抄底 / 抄顶窗口判定（对齐双源抄底逻辑）
  const isRangeLike = regime === 'RANGE' || regime === 'NEUTRAL';
  const adxCalm = adx < strongAdx;
  const buyWindow = isRangeLike && adxCalm && rsi !== null && rsi <= RSI_OVERSOLD;
  const sellWindow = isRangeLike && adxCalm && rsi !== null && rsi >= RSI_OVERBOUGHT;

  const windowTone: Tone = buyWindow || sellWindow ? 'ok' : isRangeLike ? 'warn' : 'idle';
  const windowText = buyWindow
    ? '抄底窗口开启 · 双源将自动出 BUY'
    : sellWindow
    ? '抄顶窗口开启 · 双源将自动出 SELL'
    : isRangeLike
    ? '震荡市 · RSI 未达极值，等待抄底/抄顶带'
    : '趋势市 · 双源不抄底（趋势跟随为主）';

  const rsiPct = rsi !== null ? Math.max(0, Math.min(100, rsi)) : 50;

  return (
    <Paper
      elevation={0}
      sx={{
        backgroundColor: '#111118',
        border: '1px solid #2a2a3a',
        borderRadius: 3,
        p: 2,
        mb: 3,
        position: 'relative',
        overflow: 'hidden',
        '&::before': {
          content: '""',
          position: 'absolute',
          top: 0,
          left: 0,
          right: 0,
          height: 3,
          backgroundColor: windowTone === 'ok' ? '#22c55e' : '#eab308',
        },
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 1.5 }}>
        <Typography
          variant="caption"
          sx={{
            color: '#94a3b8',
            textTransform: 'uppercase',
            letterSpacing: '0.05em',
            fontWeight: 500,
          }}
        >
          抄底 / 抄顶监控
        </Typography>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
          <Chip
            label={regimeLabel(regime)}
            size="small"
            sx={{
              backgroundColor: BG[regimeTone(regime)],
              color: FG[regimeTone(regime)],
              fontWeight: 600,
              fontSize: 10,
              height: 22,
            }}
          />
          <Typography sx={{ color: '#64748b', fontSize: 11 }}>
            {symbol} · 实时
          </Typography>
        </Box>
      </Box>

      {/* 主提示条 */}
      <Box
        sx={{
          backgroundColor: BG[windowTone],
          border: `1px solid ${FG[windowTone]}55`,
          borderRadius: 2,
          px: 2,
          py: 1.2,
          mb: 1.5,
          display: 'flex',
          alignItems: 'center',
          gap: 1,
        }}
      >
        <Typography
          sx={{
            color: FG[windowTone],
            fontWeight: 700,
            fontSize: 14,
          }}
        >
          {windowTone === 'ok' ? '🟢' : windowTone === 'warn' ? '🟡' : '⚪'} {windowText}
        </Typography>
      </Box>

      <Box sx={{ display: 'flex', gap: 2, flexWrap: 'wrap' }}>
        {/* RSI 温度计 */}
        <Box sx={{ flex: 1, minWidth: 220 }}>
          <Box sx={{ display: 'flex', justifyContent: 'space-between', mb: 0.5 }}>
            <Typography sx={{ color: '#64748b', fontSize: 11 }}>RSI(14)</Typography>
            <Typography
              sx={{
                color:
                  rsi !== null && rsi <= RSI_OVERSOLD
                    ? '#22c55e'
                    : rsi !== null && rsi >= RSI_OVERBOUGHT
                    ? '#ef4444'
                    : '#94a3b8',
                fontSize: 12,
                fontWeight: 600,
              }}
            >
              {rsi !== null ? rsi.toFixed(0) : '--'}
            </Typography>
          </Box>
          <Box
            sx={{
              position: 'relative',
              height: 10,
              borderRadius: 5,
              background:
                'linear-gradient(90deg,#22c55e 0%,#eab308 50%,#ef4444 100%)',
              opacity: 0.4,
            }}
          >
            {/* 35 抄底刻度 */}
            <Box
              sx={{
                position: 'absolute',
                left: `${RSI_OVERSOLD}%`,
                top: -2,
                bottom: -2,
                width: 1,
                backgroundColor: '#22c55e',
              }}
            />
            {/* 65 抄顶刻度 */}
            <Box
              sx={{
                position: 'absolute',
                left: `${RSI_OVERBOUGHT}%`,
                top: -2,
                bottom: -2,
                width: 1,
                backgroundColor: '#ef4444',
              }}
            />
            {/* 当前指针 */}
            <Box
              sx={{
                position: 'absolute',
                left: `calc(${rsiPct}% - 1.5px)`,
                top: -3,
                width: 3,
                height: 16,
                backgroundColor: '#e2e8f0',
                borderRadius: 1.5,
              }}
            />
          </Box>
          <Box sx={{ display: 'flex', justifyContent: 'space-between', mt: 0.3 }}>
            <Typography sx={{ color: '#22c55e', fontSize: 9 }}>≤{RSI_OVERSOLD} 抄底</Typography>
            <Typography sx={{ color: '#ef4444', fontSize: 9 }}>抄顶 ≥{RSI_OVERBOUGHT}</Typography>
          </Box>
        </Box>

        {/* ADX 指标 */}
        <Box sx={{ minWidth: 150 }}>
          <Box sx={{ display: 'flex', justifyContent: 'space-between', mb: 0.5 }}>
            <Typography sx={{ color: '#64748b', fontSize: 11 }}>ADX(14)</Typography>
            <Typography
              sx={{
                color: adx >= strongAdx ? '#3b82f6' : '#94a3b8',
                fontSize: 12,
                fontWeight: 600,
              }}
            >
              {adx.toFixed(1)}
            </Typography>
          </Box>
          <Typography sx={{ color: '#475569', fontSize: 10, lineHeight: 1.3 }}>
            {adx >= strongAdx
              ? `≥${strongAdx.toFixed(0)} 强趋势（不抄底）`
              : `<${strongAdx.toFixed(0)} 非趋势（可抄底）`}
          </Typography>
        </Box>

        {/* 条件说明 */}
        <Box sx={{ minWidth: 200, flex: 1 }}>
          <Typography sx={{ color: '#475569', fontSize: 10, lineHeight: 1.5 }}>
            {isRangeLike
              ? '行情转 RANGE/NEUTRAL + RSI 进入极值带，双源信号自动出单抄底/抄顶。'
              : '趋势市双源不抄底；逆势单会被 H1 防火墙 + calib 拦截（防接飞刀）。'}
          </Typography>
        </Box>
      </Box>

      <Tooltip
        title={
          '阈值：RSI≤35 抄底 / RSI≥65 抄顶 / ADX<24 非趋势。与后台 co.gate.range + scoring.range_rsi_extreme_* 对齐。'
        }
        arrow
      >
        <Typography
          sx={{ color: '#334155', fontSize: 9, mt: 1, cursor: 'help' }}
        >
          窗口条件：震荡市(RANGE/NEUTRAL) + RSI 极值 + ADX&lt;强趋势门槛
        </Typography>
      </Tooltip>
    </Paper>
  );
};

export default ReversalWatchCard;
