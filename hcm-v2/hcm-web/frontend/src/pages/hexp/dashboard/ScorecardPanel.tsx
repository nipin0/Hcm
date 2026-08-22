/** D 区 · 6 维评分卡 —— 雷达图 + 加权贡献明细 + 分级门槛定位 */
import React, { useMemo } from 'react';
import { Box, Typography, Tooltip } from '@mui/material';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import * as echarts from 'echarts/core';
import { RadarChart } from 'echarts/charts';
// RadarComponent 是雷达「坐标系」组件，与 RadarChart（系列）必须成对注册，
// 否则运行时报 "Component radar is used but not imported"。
import { TooltipComponent, RadarComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import {
  C, cfgNum, fmt, SCORECARD_LABELS, SCORECARD_WEIGHT_KEYS, gradeColor,
} from './types';
import type { HexpSnapshot, HexpConfig, HexpScorecard } from './types';

echarts.use([RadarChart, RadarComponent, TooltipComponent, CanvasRenderer]);

interface Props {
  snap: HexpSnapshot | null;
  cfg: HexpConfig | null;
}

const DIMS: (keyof HexpScorecard)[] = ['resonance', 'state', 'entry', 'position', 'vol', 'session'];

const ScorecardPanel: React.FC<Props> = ({ snap, cfg }) => {
  const passTh = cfgNum(cfg, 'hexp.scorecard.pass_threshold');
  const bTh = cfgNum(cfg, 'hexp.scorecard.b_threshold');
  const aTh = cfgNum(cfg, 'hexp.scorecard.a_threshold');
  const total = snap?.scorecard_total ?? 0;
  const grade = snap?.grade ?? '—';

  const rows = useMemo(() => {
    const sc = snap?.scorecard;
    const totalW = DIMS.reduce((a, d) => a + cfgNum(cfg, SCORECARD_WEIGHT_KEYS[d]), 0) || 1;
    return DIMS.map((d) => {
      const v = sc ? (sc[d] ?? 0) : 0;
      const w = cfgNum(cfg, SCORECARD_WEIGHT_KEYS[d]);
      return {
        key: d,
        label: SCORECARD_LABELS[d],
        value: v,
        weight: w,
        /** 该维对总分的实际贡献（加权平均口径） */
        contrib: (v * w) / totalW,
      };
    });
  }, [snap, cfg]);

  const radarOption = useMemo(() => ({
    tooltip: {
      backgroundColor: 'rgba(17,17,24,0.96)',
      borderColor: C.border,
      textStyle: { color: C.textMain, fontSize: 12 },
      formatter: () =>
        rows
          .map((r) => `${r.label} <b>${fmt(r.value, 1)}</b> <span style="color:${C.textFaint}">×w${fmt(r.weight, 0)} → ${fmt(r.contrib, 1)}</span>`)
          .join('<br/>'),
    },
    radar: {
      indicator: rows.map((r) => ({ name: r.label, max: 100 })),
      center: ['50%', '54%'],
      radius: '66%',
      splitNumber: 4,
      axisName: { color: C.textDim, fontSize: 10.5 },
      splitLine: { lineStyle: { color: 'rgba(148,163,184,0.14)' } },
      splitArea: { areaStyle: { color: ['rgba(148,163,184,0.03)', 'transparent'] } },
      axisLine: { lineStyle: { color: 'rgba(148,163,184,0.14)' } },
    },
    series: [{
      type: 'radar',
      symbolSize: 4,
      data: [{
        value: rows.map((r) => Number(r.value.toFixed(2))),
        name: '评分卡',
        lineStyle: { color: C.info, width: 2 },
        itemStyle: { color: C.info },
        areaStyle: { color: 'rgba(59,130,246,0.20)' },
      }],
    }],
    animationDuration: 320,
  }), [rows]);

  const gc = gradeColor(grade);

  return (
    <Box
      sx={{
        p: 2, borderRadius: 2, background: C.panelBg,
        border: `1px solid ${C.border}`, height: '100%',
        display: 'flex', flexDirection: 'column',
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1, mb: 0.5 }}>
        <Typography sx={{ fontSize: 13, fontWeight: 700, color: C.textMain }}>
          6 维评分卡
        </Typography>
        <Typography sx={{ fontSize: 10.5, color: C.textFaint }}>加权总分决定分级</Typography>
      </Box>

      <Box sx={{ display: 'flex', gap: 1.5, flex: 1, flexWrap: 'wrap' }}>
        {/* 雷达 */}
        <Box sx={{ flex: '1 1 220px', minWidth: 200, minHeight: 210 }}>
          <ReactEChartsCore
            echarts={echarts}
            option={radarOption}
            style={{ height: '100%', width: '100%', minHeight: 210 }}
            notMerge
            lazyUpdate
          />
        </Box>

        {/* 明细 */}
        <Box sx={{ flex: '1 1 200px', minWidth: 190, display: 'flex', flexDirection: 'column', gap: 0.6 }}>
          {rows.map((r) => (
            <Box key={r.key}>
              <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
                <Typography sx={{ fontSize: 10.5, color: C.textDim }}>
                  {r.label}
                  <span style={{ color: C.textFaint, fontSize: 9.5 }}> ×{fmt(r.weight, 0)}</span>
                </Typography>
                <Typography
                  sx={{ fontSize: 11, fontWeight: 700, color: C.textMain, fontVariantNumeric: 'tabular-nums' }}
                >
                  {fmt(r.value, 1)}
                </Typography>
              </Box>
              <Box sx={{ height: 4, borderRadius: 2, background: 'rgba(148,163,184,0.12)', overflow: 'hidden' }}>
                <Box
                  sx={{
                    height: '100%', borderRadius: 2,
                    width: `${Math.min(100, Math.max(0, r.value))}%`,
                    background: r.value >= 60 ? C.info : r.value >= 40 ? '#06b6d4' : C.flat,
                    transition: 'width .4s ease',
                  }}
                />
              </Box>
            </Box>
          ))}
        </Box>
      </Box>

      {/* 总分与门槛 */}
      <Box sx={{ mt: 1.5 }}>
        <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1, mb: 0.6 }}>
          <Typography sx={{ fontSize: 11, color: C.textFaint }}>加权总分</Typography>
          <Typography
            sx={{
              fontSize: 20, fontWeight: 800, color: gc,
              fontVariantNumeric: 'tabular-nums', lineHeight: 1,
            }}
          >
            {fmt(total, 1)}
          </Typography>
          <Typography sx={{ fontSize: 11, color: C.textDim }}>
            → 等级 <b style={{ color: gc }}>{grade}</b>
          </Typography>
        </Box>
        <Box sx={{ position: 'relative', height: 9, borderRadius: 5, background: 'rgba(148,163,184,0.14)' }}>
          <Box
            sx={{
              position: 'absolute', inset: '0 auto 0 0', borderRadius: 5,
              width: `${Math.min(100, Math.max(0, total))}%`,
              background: `linear-gradient(90deg, ${C.flat}, ${gc})`,
              transition: 'width .4s ease',
            }}
          />
          {[
            { v: passTh, c: C.warn, t: `放行门槛 ${fmt(passTh, 0)}` },
            { v: bTh, c: '#06b6d4', t: `B 级门槛 ${fmt(bTh, 0)}` },
            { v: aTh, c: C.info, t: `A 级门槛 ${fmt(aTh, 0)}` },
          ].map((m) => (
            <Tooltip key={m.t} title={m.t} arrow>
              <Box
                sx={{
                  position: 'absolute', top: -3, bottom: -3, width: 2,
                  left: `${Math.min(100, Math.max(0, m.v))}%`, background: m.c,
                }}
              />
            </Tooltip>
          ))}
        </Box>
      </Box>
    </Box>
  );
};

export default ScorecardPanel;
