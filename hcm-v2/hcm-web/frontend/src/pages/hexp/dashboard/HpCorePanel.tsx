/** B 区 · 和乘幂核心 —— 幂指数 k 自适应刻度 + 7 因子贡献分解 */
import React, { useMemo } from 'react';
import { Box, Typography, Tooltip } from '@mui/material';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import * as echarts from 'echarts/core';
import { BarChart } from 'echarts/charts';
import { GridComponent, TooltipComponent, MarkLineComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import {
  C, cfgNum, kRegime, fmt, fmtSigned, FACTOR_LABELS, FACTOR_WEIGHT_KEYS,
  factorRawText,
} from './types';
import type { HexpSnapshot, HexpConfig, HexpFactorScores } from './types';

echarts.use([BarChart, GridComponent, TooltipComponent, MarkLineComponent, CanvasRenderer]);

interface Props {
  snap: HexpSnapshot | null;
  cfg: HexpConfig | null;
}

const FACTOR_ORDER: (keyof HexpFactorScores)[] = ['adx', 'er', 'ma', 'bbw', 'hurst', 'rsi', 'mm'];

const HpCorePanel: React.FC<Props> = ({ snap, cfg }) => {
  const kMin = cfgNum(cfg, 'hexp.k.min');
  const kMax = cfgNum(cfg, 'hexp.k.max');
  const k = snap?.k ?? cfgNum(cfg, 'hexp.k.base');
  const reg = kRegime(k);
  const kPct = Math.min(100, Math.max(0, ((k - kMin) / Math.max(1e-6, kMax - kMin)) * 100));

  /** 因子加权贡献：|f|^k × w，用于说明「和乘幂」如何放大/收敛 */
  const rows = useMemo(() => {
    const fs = snap?.factor_scores;
    const fr = snap?.factor_raws;
    return FACTOR_ORDER.map((key) => {
      const raw = fs ? (fs[key] ?? 0) : 0;
      const w = cfgNum(cfg, FACTOR_WEIGHT_KEYS[key]);
      const powered = Math.pow(Math.abs(raw), k) * w;
      const rawVal = fr ? (fr[key] ?? null) : null;
      return { key, label: FACTOR_LABELS[key], raw, w, powered, rawVal };
    });
  }, [snap, cfg, k]);

  const sumPowered = rows.reduce((a, r) => a + r.powered, 0) || 1;

  const barOption = useMemo(() => {
    const cats = rows.map((r) => r.label.split(' ')[0]);
    const vals = rows.map((r) => Number(r.raw.toFixed(4)));
    return {
      grid: { left: 52, right: 26, top: 8, bottom: 20 },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        backgroundColor: 'rgba(17,17,24,0.96)',
        borderColor: C.border,
        textStyle: { color: C.textMain, fontSize: 12 },
        formatter: (params: unknown) => {
          const arr = params as { dataIndex: number }[];
          const idx = arr?.[0]?.dataIndex ?? 0;
          const r = rows[idx];
          if (!r) return '';
          const share = ((r.powered / sumPowered) * 100).toFixed(1);
          const rawTxt = factorRawText(r.key, r.rawVal);
          return (
            `<b>${r.label}</b><br/>` +
            `实时值 <b style="color:${C.info}">${rawTxt}</b><br/>` +
            `原始得分 <b style="color:${r.raw >= 0 ? C.up : C.down}">${fmtSigned(r.raw, 3)}</b><br/>` +
            `基础权重 ${fmt(r.w, 1)}<br/>` +
            `幂化贡献 |f|<sup>k</sup>×w = ${fmt(r.powered, 2)}<br/>` +
            `占比 <b>${share}%</b>`
          );
        },
      },
      xAxis: {
        type: 'value', min: -1, max: 1,
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { lineStyle: { color: 'rgba(148,163,184,0.10)' } },
        axisLabel: { color: C.textFaint, fontSize: 10 },
      },
      yAxis: {
        type: 'category',
        data: cats,
        inverse: true,
        axisLine: { lineStyle: { color: C.border } },
        axisTick: { show: false },
        axisLabel: { color: C.textDim, fontSize: 11 },
      },
      series: [{
        type: 'bar',
        data: vals.map((v) => ({
          value: v,
          itemStyle: {
            color: v >= 0 ? C.up : C.down,
            borderRadius: v >= 0 ? [0, 3, 3, 0] : [3, 0, 0, 3],
            opacity: 0.88,
          },
        })),
        barWidth: 11,
        markLine: {
          silent: true,
          symbol: 'none',
          data: [{ xAxis: 0 }],
          lineStyle: { color: 'rgba(148,163,184,0.5)', width: 1, type: 'solid' },
          label: { show: false },
        },
      }],
      animationDuration: 320,
    };
  }, [rows, sumPowered]);

  return (
    <Box
      sx={{
        p: 2, borderRadius: 2, background: C.panelBg,
        border: `1px solid ${C.border}`, height: '100%',
        display: 'flex', flexDirection: 'column',
      }}
    >
      <Typography sx={{ fontSize: 13, fontWeight: 700, color: C.textMain, mb: 0.3 }}>
        和乘幂核心
      </Typography>
      <Typography sx={{ fontSize: 10.5, color: C.textFaint, mb: 1.5, fontFamily: 'monospace' }}>
        HP = (Σ wᵢ·|fᵢ|<sup>k</sup>)<sup>1/k</sup> × sign(Σ wᵢ·fᵢ)
      </Typography>

      {/* 幂指数 k 刻度条 */}
      <Box sx={{ mb: 2 }}>
        <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1, mb: 0.8 }}>
          <Typography sx={{ fontSize: 11, color: C.textFaint }}>幂指数 k</Typography>
          <Typography
            sx={{
              fontSize: 22, fontWeight: 800, color: reg.color,
              fontVariantNumeric: 'tabular-nums', lineHeight: 1,
            }}
          >
            {fmt(k, 3)}
          </Typography>
          <Box
            sx={{
              px: 0.9, py: 0.2, borderRadius: 0.8, fontSize: 10.5, fontWeight: 700,
              color: reg.color, border: `1px solid ${reg.color}66`, background: `${reg.color}14`,
            }}
          >
            {reg.label}
          </Box>
        </Box>

        <Box sx={{ position: 'relative', height: 22 }}>
          {/* 三段底色：凹收敛 / 线性 / 凸增强 */}
          <Box sx={{ position: 'absolute', inset: '6px 0 auto 0', height: 8, borderRadius: 4, overflow: 'hidden', display: 'flex' }}>
            <Box sx={{ flex: (0.85 - kMin), background: `${C.down}55` }} />
            <Box sx={{ flex: 0.3, background: `${C.info}55` }} />
            <Box sx={{ flex: (kMax - 1.15), background: `${C.up}55` }} />
          </Box>
          {/* 当前 k 指针 */}
          <Tooltip title={reg.desc} arrow>
            <Box
              sx={{
                position: 'absolute', top: 0, left: `${kPct}%`,
                transform: 'translateX(-50%)',
                width: 3, height: 20, borderRadius: 1.5,
                background: reg.color, boxShadow: `0 0 8px ${reg.color}`,
                transition: 'left .4s ease',
              }}
            />
          </Tooltip>
        </Box>
        <Box sx={{ display: 'flex', justifyContent: 'space-between', mt: 0.2 }}>
          <Typography sx={{ fontSize: 9.5, color: C.textFaint }}>{fmt(kMin, 1)} 凹收敛·震荡</Typography>
          <Typography sx={{ fontSize: 9.5, color: C.textFaint }}>1.0 线性</Typography>
          <Typography sx={{ fontSize: 9.5, color: C.textFaint }}>{fmt(kMax, 1)} 凸增强·趋势</Typography>
        </Box>
      </Box>

      {/* 因子贡献 */}
      <Typography sx={{ fontSize: 11, color: C.textFaint, mb: 0.5 }}>
        因子原始得分（红=偏多 / 绿=偏空），悬停看幂化贡献占比
      </Typography>
      <Box sx={{ flex: 1, minHeight: 168 }}>
        <ReactEChartsCore
          echarts={echarts}
          option={barOption}
          style={{ height: '100%', width: '100%', minHeight: 168 }}
          notMerge
          lazyUpdate
        />
      </Box>

      {/* 7 因子实时原始数值（ADX/RSI/Hurst 等指标本身读数，非归一方向分） */}
      <Box
        sx={{
          mt: 1.5, pt: 1, borderTop: `1px solid ${C.border}`,
          display: 'grid', gridTemplateColumns: 'repeat(7, 1fr)', gap: 0.5,
        }}
      >
        {rows.map((r) => {
          const dirColor = r.raw > 0.01 ? C.up : (r.raw < -0.01 ? C.down : C.flat);
          return (
            <Tooltip
              key={r.key}
              arrow
              title={`${r.label} · 实时值 ${factorRawText(r.key, r.rawVal)} · 方向分 ${fmtSigned(r.raw, 3)}`}
            >
              <Box sx={{ textAlign: 'center', py: 0.5, borderRadius: 0.8, '&:hover': { background: C.flatSoft } }}>
                <Typography sx={{ fontSize: 9, color: C.textFaint, lineHeight: 1.2, letterSpacing: 0.4 }}>
                  {r.key.toUpperCase()}
                </Typography>
                <Typography
                  sx={{
                    fontSize: 11.5, fontWeight: 700, color: dirColor,
                    fontVariantNumeric: 'tabular-nums', lineHeight: 1.25,
                  }}
                >
                  {factorRawText(r.key, r.rawVal)}
                </Typography>
              </Box>
            </Tooltip>
          );
        })}
      </Box>
    </Box>
  );
};

export default HpCorePanel;
