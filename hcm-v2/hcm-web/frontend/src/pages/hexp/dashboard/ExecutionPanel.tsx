/** F 区 · 执行预案 —— 等级手数系数 / SL / TP / 降仓规则
 *
 * 所有系数均从配置中心 hexp.exec.* 读取（零硬编码）。
 * 当 direction=NO_TRADE 时，按主周期趋势给出「假设方向」的预演（灰显标注）。
 */
import React from 'react';
import { Box, Typography, Tooltip } from '@mui/material';
import { C, cfgNum, fmt, dirColor, gradeColor, directionDiag } from './types';
import type { HexpSnapshot, HexpConfig } from './types';

interface Props {
  snap: HexpSnapshot | null;
  cfg: HexpConfig | null;
}

const Row: React.FC<{ label: string; value: React.ReactNode; hint?: string }> = ({
  label, value, hint,
}) => (
  <Tooltip title={hint || ''} arrow disableHoverListener={!hint}>
    <Box
      sx={{
        display: 'flex', justifyContent: 'space-between', alignItems: 'baseline',
        py: 0.55, borderBottom: `1px solid ${C.border}`,
      }}
    >
      <Typography sx={{ fontSize: 11, color: C.textDim }}>{label}</Typography>
      <Typography
        sx={{ fontSize: 12.5, fontWeight: 700, color: C.textMain, fontVariantNumeric: 'tabular-nums' }}
      >
        {value}
      </Typography>
    </Box>
  </Tooltip>
);

const ExecutionPanel: React.FC<Props> = ({ snap, cfg }) => {
  const slMult = cfgNum(cfg, 'hexp.exec.sl_atr_mult');
  const rrMin = cfgNum(cfg, 'hexp.exec.rr_min');
  const lotMult = cfgNum(cfg, 'hexp.exec.lot_mult');
  const transMult = cfgNum(cfg, 'hexp.exec.transition_lot_mult');

  const grade = snap?.grade ?? '—';
  const gradeLotKey: Record<string, string> = {
    S: 'hexp.exec.grade_lot_s',
    A: 'hexp.exec.grade_lot_a',
    B: 'hexp.exec.grade_lot_b',
    C: 'hexp.exec.grade_lot_c',
  };
  const gradeLot = gradeLotKey[grade] ? cfgNum(cfg, gradeLotKey[grade]) : 0;

  const primary = snap?.primary_period ?? 'M5';
  const pState = (snap?.period_states ?? {})[primary] ?? '';
  const isTransition = pState === 'TRANSITION';

  const realDir = snap?.direction ?? 'NO_TRADE';
  /** NO_TRADE 时按主周期趋势做「假设方向」预演 */
  const assumed = realDir === 'NO_TRADE';
  const effDir = !assumed
    ? realDir
    : pState === 'TREND_UP' ? 'BUY' : pState === 'TREND_DOWN' ? 'SELL' : 'BUY';

  const close = snap?.close ?? 0;
  const atr = snap?.atr ?? 0;
  const slDist = atr * slMult;
  const tpDist = slDist * rrMin;
  const isBuy = effDir === 'BUY';
  const sl = close ? (isBuy ? close - slDist : close + slDist) : 0;
  const tp = close ? (isBuy ? close + tpDist : close - tpDist) : 0;

  const finalLot = gradeLot * lotMult * (isTransition ? transMult : 1);
  const dc = dirColor(effDir);
  const gc = gradeColor(grade);
  // 2026-08-27 方向诊断：基于真实方向（realDir）显示迟滞/死标签/翻转状态
  const diag = directionDiag(snap);

  return (
    <Box
      sx={{
        p: 2, borderRadius: 2, background: C.panelBg,
        border: `1px solid ${C.border}`, height: '100%',
        display: 'flex', flexDirection: 'column',
        opacity: assumed ? 0.82 : 1,
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.8, mb: 1 }}>
        <Typography sx={{ fontSize: 13, fontWeight: 700, color: C.textMain }}>
          执行预案
        </Typography>
        {assumed && (
          <Tooltip
            arrow
            title="当前引擎判定 NO_TRADE。此处按主周期趋势方向做「若放行」的参数预演，非实际挂单"
          >
            <Box
              sx={{
                px: 0.7, py: 0.1, borderRadius: 0.6, fontSize: 9.5, fontWeight: 700,
                color: C.textDim, border: `1px solid ${C.border}`, cursor: 'help',
              }}
            >
              假设方向预演
            </Box>
          </Tooltip>
        )}
      </Box>

      <Box sx={{ display: 'flex', gap: 1, mb: 1.2 }}>
        <Box
          sx={{
            flex: 1, p: 1, borderRadius: 1.5, textAlign: 'center',
            background: `${dc}12`, border: `1px solid ${dc}55`,
          }}
        >
          <Typography sx={{ fontSize: 10, color: C.textFaint }}>方向</Typography>
          <Typography sx={{ fontSize: 15, fontWeight: 800, color: dc }}>
            {isBuy ? 'BUY' : 'SELL'}
          </Typography>
          {/* 2026-08-27 真实方向诊断（迟滞/死标签/翻转） */}
          <Box
            sx={{
              mt: 0.3, px: 0.6, py: 0.1, borderRadius: 0.6, fontSize: 8.5, fontWeight: 700,
              color: diag.color, border: `1px solid ${diag.color}55`, background: `${diag.color}10`,
            }}
          >
            {diag.tag}
          </Box>
        </Box>
        <Box
          sx={{
            flex: 1, p: 1, borderRadius: 1.5, textAlign: 'center',
            background: `${gc}12`, border: `1px solid ${gc}55`,
          }}
        >
          <Typography sx={{ fontSize: 10, color: C.textFaint }}>手数系数</Typography>
          <Typography sx={{ fontSize: 15, fontWeight: 800, color: gc, fontVariantNumeric: 'tabular-nums' }}>
            ×{fmt(finalLot, 2)}
          </Typography>
        </Box>
      </Box>

      <Row
        label="入场参考价"
        value={fmt(close, 2)}
        hint="引擎快照收盘价，实际成交以桥下单时行情为准"
      />
      <Row
        label={`止损 SL（ATR×${fmt(slMult, 1)}）`}
        value={<span style={{ color: C.down }}>{fmt(sl, 2)}</span>}
        hint={`距离 ${fmt(slDist, 2)}`}
      />
      <Row
        label={`止盈 TP（RR≥${fmt(rrMin, 1)}）`}
        value={<span style={{ color: C.up }}>{fmt(tp, 2)}</span>}
        hint={`距离 ${fmt(tpDist, 2)}`}
      />
      <Row label={`等级 ${grade} 基础系数`} value={`×${fmt(gradeLot, 2)}`} />
      <Row label="全局手数系数" value={`×${fmt(lotMult, 2)}`} />
      <Row
        label="转换态降仓"
        value={
          isTransition
            ? <span style={{ color: C.warn }}>已生效 ×{fmt(transMult, 2)}</span>
            : <span style={{ color: C.textFaint }}>未触发</span>
        }
        hint="主周期处于 TRANSITION 状态时自动降仓"
      />

      <Typography sx={{ fontSize: 9.5, color: C.textFaint, mt: 'auto', pt: 1, lineHeight: 1.5 }}>
        参数源：配置中心 <code>hexp.exec.*</code>，与引擎同一份配置，改参即时同步。
      </Typography>
    </Box>
  );
};

export default ExecutionPanel;
