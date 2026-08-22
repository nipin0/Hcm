import React from 'react';
import { Box, Typography, Paper, LinearProgress, Tooltip, Chip } from '@mui/material';
import type { LatestInference, ScoringThresholds } from '../../types/signalGauge';

/** Format a duration in seconds to a human-readable age string */
function formatSignalAge(isoStr: string | null | undefined): string {
  if (!isoStr) return '--';
  const then = new Date(isoStr).getTime();
  if (isNaN(then)) return '--';
  const diffSec = Math.floor((Date.now() - then) / 1000);
  if (diffSec < 5) return '刚刚';
  if (diffSec < 60) return `${diffSec}s`;
  const mins = Math.floor(diffSec / 60);
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  return `${hours}h`;
}

interface ModelStatusPanelProps {
  inference: LatestInference | null;
  thresholds: ScoringThresholds | null;
  /** Optional: AI circuit breaker state (CLOSED / HALF_OPEN / OPEN) */
  aiCircuitState?: string;
}

type StatusTone = 'ok' | 'warn' | 'block' | 'idle' | 'info';

const STATUS_BG: Record<StatusTone, string> = {
  ok: 'rgba(34,197,94,0.15)',
  warn: 'rgba(234,179,8,0.15)',
  block: 'rgba(239,68,68,0.15)',
  idle: 'rgba(100,116,139,0.15)',
  info: 'rgba(59,130,246,0.15)',
};
const STATUS_FG: Record<StatusTone, string> = {
  ok: '#22c55e',
  warn: '#eab308',
  block: '#ef4444',
  idle: '#94a3b8',
  info: '#3b82f6',
};

interface ModelRowProps {
  icon: string;
  iconColor: string;
  name: string;
  value: string;
  valueSub?: string;
  progressPct?: number;
  badgeText: string;
  badgeTone: StatusTone;
  tooltip?: string;
}

const ModelRow: React.FC<ModelRowProps> = ({
  icon, iconColor, name, value, valueSub, progressPct, badgeText, badgeTone, tooltip,
}) => {
  const content = (
    <Box
      sx={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        gap: 1.5, py: 1.1, borderBottom: '1px solid #1e293b',
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.4, flex: 1, minWidth: 0 }}>
        <Box
          sx={{
            width: 32, height: 32, borderRadius: 1.5,
            backgroundColor: `${iconColor}22`,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            flexShrink: 0,
          }}
        >
          <Typography sx={{ color: iconColor, fontWeight: 600, fontSize: 11 }}>{icon}</Typography>
        </Box>
        <Box sx={{ minWidth: 0, flex: 1 }}>
          <Typography sx={{ color: '#94a3b8', fontSize: 11, lineHeight: 1.2 }}>{name}</Typography>
          <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 0.8, mt: 0.3, flexWrap: 'wrap' }}>
            <Typography sx={{ color: '#e2e8f0', fontWeight: 600, fontSize: 13 }}>{value}</Typography>
            {valueSub && (
              <Typography sx={{ color: '#64748b', fontSize: 11 }}>{valueSub}</Typography>
            )}
          </Box>
          {progressPct !== undefined && (
            <LinearProgress
              variant="determinate"
              value={Math.max(0, Math.min(100, progressPct))}
              sx={{
                mt: 0.6, height: 3, borderRadius: 1.5,
                backgroundColor: '#1e293b',
                '& .MuiLinearProgress-bar': {
                  background: 'linear-gradient(90deg, #22c55e 0%, #3b82f6 100%)',
                  borderRadius: 1.5,
                },
              }}
            />
          )}
        </Box>
      </Box>
      <Chip
        label={badgeText}
        size="small"
        sx={{
          backgroundColor: STATUS_BG[badgeTone],
          color: STATUS_FG[badgeTone],
          fontWeight: 600, fontSize: 10, height: 22,
          '& .MuiChip-label': { px: 1 },
        }}
      />
    </Box>
  );
  return tooltip ? <Tooltip title={tooltip} arrow placement="left">{content}</Tooltip> : content;
};

const ModelStatusPanel: React.FC<ModelStatusPanelProps> = ({
  inference, thresholds, aiCircuitState = 'CLOSED',
}) => {
  if (!inference) {
    return (
      <Paper
        elevation={0}
        sx={{
          backgroundColor: '#111118', border: '1px solid #2a2a3a', borderRadius: 3, p: 2.5,
        }}
      >
        <Typography variant="caption" sx={{ color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 500 }}>
          5 模型运行状态
        </Typography>
        <Typography sx={{ color: '#64748b', fontSize: 13, mt: 1.5 }}>暂无信号</Typography>
      </Paper>
    );
  }

  // B1 fix: prefer live ADX (from factor gauges) over the signal-snapshot ADX.
  // The signal's `adx_14` is the value when the signal was generated (1+ min
  // ago for M5); `live_adx_14` is the current value and matches the gauge
  // chart at the bottom. Without this, the panel and the gauge disagreed.
  const adx = inference.live_adx_14 || inference.adx_14 || 0;
  const adxThreshold = thresholds?.strong_adx_threshold || 24;
  const adxActive = adx >= adxThreshold;

  const score = inference.pre_score || inference.score || 0;
  const minThreshold = 0.15;
  const baseThreshold = thresholds?.base || 0.10;
  const passThreshold = adxActive ? baseThreshold : minThreshold;
  const passPct = Math.min(100, (score / Math.max(passThreshold, 0.01)) * 100);
  const scorePass = score >= passThreshold;
  const aiCalled = inference.ai_sl_mult > 0 || inference.ai_tp_mult > 0;
  const circuitOpen = aiCircuitState === 'OPEN';

  const direction = inference.direction;
  // B3 fix: when direction=NO_TRADE due to adx_floor, the score is "high but
  // blocked" — don't show the misleading "已过" (passed) hint.
  const blockedByAdxFloor = (inference.fallback_reason || '').includes('adx_floor');
  const scoreBadge = direction === 'NO_TRADE' && blockedByAdxFloor
    ? 'ADX 阻断'
    : direction;
  const directionColor: StatusTone =
    direction === 'BUY' ? 'ok' : direction === 'SELL' ? 'info' : 'idle';

  const regime = inference.regime;
  const regimeTone: StatusTone =
    regime === 'TREND' ? 'ok' : regime === 'RANGE' ? 'warn' :
    regime === 'TREND_FADE' ? 'info' : 'idle';

  const zoneType = inference.zone_type;
  const zoneLevel = inference.zone_level;
  const zoneStr = inference.zone_strength;
  const zoneB = zoneStr >= 3;
  const zoneTone: StatusTone = zoneB ? 'warn' : 'idle';

  const slConsumed = inference.ai_sl_mult > 0;
  const trigger = inference.entry_trigger_wait;

  return (
    <Paper
      elevation={0}
      sx={{
        backgroundColor: '#111118', border: '1px solid #2a2a3a', borderRadius: 3, p: 2.5,
        minWidth: 280, position: 'relative', overflow: 'hidden',
        '&::before': {
          content: '""', position: 'absolute', top: 0, left: 0, right: 0, height: 3,
          backgroundColor: '#3b82f6', borderTopLeftRadius: 3, borderTopRightRadius: 3,
        },
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', mb: 0.5 }}>
        <Typography variant="caption" sx={{ color: '#94a3b8', textTransform: 'uppercase', letterSpacing: '0.05em', fontWeight: 500 }}>
          5 模型运行状态
        </Typography>
        <Typography sx={{ color: '#64748b', fontSize: 11 }}>
          {inference.symbol || '--'} · {inference.timeframe || '--'}
        </Typography>
      </Box>

      <ModelRow
        icon="ADX"
        iconColor="#3b82f6"
        name="① 传统指标 · ADX"
        value={adx.toFixed(1)}
        valueSub={`/ ≥${adxThreshold} ${adxActive ? '已触发' : '未达'}`}
        badgeText={adxActive ? '已激活' : '静默'}
        badgeTone={adxActive ? 'ok' : 'idle'}
        tooltip={`ADX ${adx.toFixed(1)} vs 阈值 ${adxThreshold} | ${adxActive ? '趋势市有方向' : '震荡/无方向'}`}
      />
      <ModelRow
        icon="SCORE"
        iconColor="#22c55e"
        name="② 评分引擎"
        value={score.toFixed(3)}
        valueSub={blockedByAdxFloor
          ? `≥ ${passThreshold.toFixed(3)} 阈值已过，但 ADX<22 阻断`
          : `≥ ${passThreshold.toFixed(3)} 阈值 ${scorePass ? '已过' : '未过'}`}
        progressPct={passPct}
        badgeText={scoreBadge}
        badgeTone={blockedByAdxFloor ? 'warn' : directionColor}
        tooltip={`weight_scheme: ${inference.weight_scheme || '--'}`}
      />
      <ModelRow
        icon="AI"
        iconColor="#a855f7"
        name="③ AI / DeepSeek 门控"
        value={aiCalled ? `SELL/${inference.ai_sl_mult.toFixed(2)}|${inference.ai_tp_mult.toFixed(2)}` : '未触发'}
        valueSub={`熔断: ${aiCircuitState} | conf ${(inference.confidence || 0).toFixed(2)}`}
        badgeText={circuitOpen ? '熔断' : aiCalled ? '已消费' : '未触发'}
        badgeTone={circuitOpen ? 'block' : aiCalled ? 'ok' : 'warn'}
        tooltip={circuitOpen
          ? 'AI 连续失败次数超限，已绕过 DeepSeek'
          : aiCalled
            ? `sl_mult=${inference.ai_sl_mult.toFixed(2)} tp_mult=${inference.ai_tp_mult.toFixed(2)}`
            : '本轮 scoring 方向清晰，AI 未被调用'}
      />
      <ModelRow
        icon="REGIME"
        iconColor="#639928"
        name="④ 市场状态识别"
        value={regime || 'NEUTRAL'}
        valueSub={`ADX=${adx.toFixed(1)} | 体制特定阈值`}
        badgeText={regimeTone === 'ok' ? '趋势' : regimeTone === 'warn' ? '震荡' : regimeTone === 'info' ? '减弱' : '中性'}
        badgeTone={regimeTone}
        tooltip={`regime=${regime}, adx_14=${adx.toFixed(2)}`}
      />
      <ModelRow
        icon="ZONE"
        iconColor="#f59e0b"
        name="⑤ Zone 结构 · P0/B路"
        value={zoneType ? `${zoneType} ${zoneLevel.toFixed(2)}` : '无'}
        valueSub={`ms=${zoneStr} | ${zoneB ? 'B路命中(H1)' : zoneStr >= 2 ? 'M15' : 'M5'}`}
        badgeText={zoneB ? 'B路命中' : zoneStr > 0 ? 'A路' : '无结构'}
        badgeTone={zoneTone}
        tooltip={`zone_level=${zoneLevel} zone_type=${zoneType} zone_strength=${zoneStr}`}
      />

      <Box sx={{ display: 'flex', justifyContent: 'space-between', mt: 1.5, pt: 1, borderTop: '1px solid #1e293b' }}>
        <Tooltip title={`sl/tp 乘数: ${inference.ai_sl_mult.toFixed(2)} / ${inference.ai_tp_mult.toFixed(2)}`} arrow>
          <Typography sx={{ color: '#64748b', fontSize: 10 }}>
            sl/tp:{' '}
            <Box component="span" sx={{ color: slConsumed ? '#22c55e' : '#f59e0b', fontWeight: 600 }}>
              {slConsumed ? `${inference.ai_sl_mult.toFixed(2)}/${inference.ai_tp_mult.toFixed(2)}` : '未消费'}
            </Box>
          </Typography>
        </Tooltip>
        <Tooltip title={`bridge zone trigger timeout: ${trigger}s`} arrow>
          <Typography sx={{ color: '#64748b', fontSize: 10 }}>
            trigger:{' '}
            <Box component="span" sx={{ color: trigger > 0 ? '#f59e0b' : '#64748b', fontWeight: 600 }}>
              {trigger > 0 ? `${trigger}s` : 'off'}
            </Box>
          </Typography>
        </Tooltip>
        <Typography sx={{ color: '#64748b', fontSize: 10 }}>{formatSignalAge(inference.updated_at)}</Typography>
      </Box>

      {inference.fallback_reason && (
        <Tooltip title={inference.fallback_reason} arrow>
          <Typography sx={{ color: '#475569', fontSize: 10, mt: 1, fontStyle: 'italic', cursor: 'help' }} noWrap>
            抑制: {inference.fallback_reason}
          </Typography>
        </Tooltip>
      )}
    </Paper>
  );
};

export default ModelStatusPanel;
