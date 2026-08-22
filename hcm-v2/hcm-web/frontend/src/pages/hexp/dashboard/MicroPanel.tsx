/** E 区 · 微结构动量 MM + 入场模式推断
 *
 * ⚠ 入场模式（回踩 A / 突破 B）**不是引擎输出字段**，是看板侧启发式推断，
 *   仅供观察，不参与下单决策。UI 已显式标注「推断」。
 */
import React from 'react';
import { Box, Typography, Tooltip } from '@mui/material';
import { C, cfgNum, fmt, fmtSigned, inferEntryMode } from './types';
import type { HexpSnapshot, HexpConfig } from './types';

interface Props {
  snap: HexpSnapshot | null;
  cfg: HexpConfig | null;
}

/** 动量三态：加速 / 衰竭 / 反转 */
function momentumState(
  mmNorm: number,
  aligned: boolean,
  accelTh: number,
): { label: string; color: string; desc: string } {
  const a = Math.abs(mmNorm);
  if (aligned && a >= accelTh) {
    return { label: '动量加速', color: mmNorm > 0 ? C.up : C.down, desc: '顺势动量突破加速阈值，趋势自我强化' };
  }
  if (!aligned && a >= accelTh) {
    return { label: '动量反转', color: C.warn, desc: '逆势动量已达加速阈值，警惕趋势反转' };
  }
  if (a < 0.25) {
    return { label: '动量衰竭', color: C.textDim, desc: '微结构动量接近枯竭，方向缺乏推动力' };
  }
  return { label: '动量温和', color: C.info, desc: '动量存在但未达加速阈值' };
}

const ScoreBar: React.FC<{ label: string; value: number; color: string; active: boolean }> = ({
  label, value, color, active,
}) => (
  <Box sx={{ mb: 0.8 }}>
    <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 1 }}>
      <Typography
        sx={{
          fontSize: 10.5, color: active ? color : C.textDim, fontWeight: active ? 700 : 400,
          minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
        }}
      >
        {label}
      </Typography>
      <Typography
        sx={{ fontSize: 10.5, color: C.textDim, fontVariantNumeric: 'tabular-nums', flexShrink: 0 }}
      >
        {fmt(value, 2)}
      </Typography>
    </Box>
    {/* 轨道：明确相对定位 + 100% 宽，防止在网格拉伸时被撑出卡片 */}
    <Box
      sx={{
        position: 'relative', width: '100%', height: 5, borderRadius: 3,
        background: 'rgba(148,163,184,0.12)', overflow: 'hidden',
      }}
    >
      <Box
        sx={{
          position: 'absolute', left: 0, top: 0, height: '100%', borderRadius: 3,
          width: `${Math.min(100, Math.max(0, value * 100))}%`,
          maxWidth: '100%',
          background: active ? color : C.flat,
          opacity: active ? 1 : 0.5,
          transition: 'width .4s ease',
        }}
      />
    </Box>
  </Box>
);

const MicroPanel: React.FC<Props> = ({ snap, cfg }) => {
  const accelTh = cfgNum(cfg, 'hexp.mm.accel_threshold');
  const scale = cfgNum(cfg, 'hexp.mm.scale') || 0.002;
  const inf = inferEntryMode(snap, cfg);
  const mmNorm = inf.mmNorm;
  const ms = momentumState(mmNorm, inf.aligned, accelTh);

  /** -1~1 映射到 0~100 */
  const pos = (v: number): number => Math.min(100, Math.max(0, ((v + 1) / 2) * 100));

  return (
    <Box
      sx={{
        p: 2, borderRadius: 2, background: C.panelBg,
        border: `1px solid ${C.border}`, height: '100%',
        display: 'flex', flexDirection: 'column',
      }}
    >
      <Typography sx={{ fontSize: 13, fontWeight: 700, color: C.textMain, mb: 1.2 }}>
        微结构动量 · 入场模式
      </Typography>

      {/* MM 归一刻度 */}
      <Box sx={{ mb: 1.5 }}>
        <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1, mb: 0.7 }}>
          <Typography sx={{ fontSize: 11, color: C.textFaint }}>MM(M1)</Typography>
          <Typography
            sx={{
              fontSize: 18, fontWeight: 800, color: ms.color,
              fontVariantNumeric: 'tabular-nums', lineHeight: 1,
            }}
          >
            {fmtSigned(mmNorm, 2)}
          </Typography>
          <Tooltip title={ms.desc} arrow>
            <Box
              sx={{
                px: 0.8, py: 0.15, borderRadius: 0.8, fontSize: 10, fontWeight: 700,
                color: ms.color, border: `1px solid ${ms.color}66`, background: `${ms.color}14`,
              }}
            >
              {ms.label}
            </Box>
          </Tooltip>
        </Box>

        {/* MM 归一刻度：内轨左右各内缩 1.5px(=指针半宽)，指针 left:% 相对内轨 +
            translateX(-50%) → 任意极端值指针都完整停在轨道内，不依赖 clamp 混合单位 */}
        <Box sx={{ position: 'relative', height: 20, overflow: 'hidden' }}>
          <Box sx={{ position: 'absolute', top: 0, bottom: 0, left: 1.5, right: 1.5 }}>
            <Box sx={{ position: 'absolute', inset: '7px 0 auto 0', height: 7, borderRadius: 4, background: 'rgba(148,163,184,0.14)' }} />
            {/* 加速阈值双侧刻度（相对内轨定位，内轨已内缩，刻度始终在 [0,100%] 内） */}
            {[-accelTh, accelTh].map((t) => (
              <Tooltip key={t} title={`加速阈值 ${fmtSigned(t, 2)}`} arrow>
                <Box
                  sx={{
                    position: 'absolute', top: 4, bottom: 4, width: 2,
                    left: `${pos(t)}%`, background: C.warn, opacity: 0.75,
                  }}
                />
              </Tooltip>
            ))}
            {/* 中轴 */}
            <Box sx={{ position: 'absolute', top: 5, bottom: 5, width: 1, left: 'calc(50% - 0.5px)', background: 'rgba(148,163,184,0.45)' }} />
            {/* 指针：left:% 相对内轨，内轨已内缩 1.5px，translateX(-50%) 后指针半宽恰好落在内轨边缘，永不溢出两端 */}
            <Box
              sx={{
                position: 'absolute', top: 1,
                left: `${pos(mmNorm)}%`,
                transform: 'translateX(-50%)',
                width: 3, height: 18, borderRadius: 1.5,
                background: ms.color, boxShadow: `0 0 8px ${ms.color}`,
                transition: 'left .4s ease',
              }}
            />
          </Box>
        </Box>
        <Typography sx={{ fontSize: 9.5, color: C.textFaint }}>
          原始 mm={snap ? snap.mm.toExponential(2) : '—'} · 归一尺度 {fmt(scale, 4)}
        </Typography>
      </Box>

      {/* 入场模式推断 */}
      <Box
        sx={{
          p: 1.2, borderRadius: 1.5, background: `${inf.color}0F`,
          border: `1px dashed ${inf.color}66`, mt: 'auto',
        }}
      >
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.8, mb: 0.8 }}>
          <Typography sx={{ fontSize: 13, fontWeight: 800, color: inf.color }}>
            {inf.label}
          </Typography>
          <Tooltip
            arrow
            title="引擎 hexp_engine 未输出 entry_mode 字段；此结论由看板依据动量/趋势/RSI 启发式推断，仅供观察，不参与下单"
          >
            <Box
              sx={{
                px: 0.7, py: 0.1, borderRadius: 0.6, fontSize: 9.5, fontWeight: 700,
                color: C.warn, border: `1px solid ${C.warn}66`, background: `${C.warn}14`,
                cursor: 'help',
              }}
            >
              ⚠ 前端推断
            </Box>
          </Tooltip>
        </Box>

        <ScoreBar
          label="模式 A · 回踩承接"
          value={inf.pullbackScore}
          color={C.warn}
          active={inf.mode === 'A_PULLBACK'}
        />
        <ScoreBar
          label="模式 B · 突破追进"
          value={inf.breakoutScore}
          color={inf.mode === 'B_BREAKOUT' ? inf.color : C.info}
          active={inf.mode === 'B_BREAKOUT'}
        />

        <Typography sx={{ fontSize: 10, color: C.textDim, mt: 0.6, lineHeight: 1.5 }}>
          {inf.rationale}
        </Typography>
      </Box>
    </Box>
  );
};

export default MicroPanel;
