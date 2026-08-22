/** C 区 · 多周期共振矩阵 —— 各周期状态/趋势分/权重 + 共振裁决滑杆 + K 线入库实时状态 */
import React from 'react';
import { Box, Typography, Tooltip } from '@mui/material';
import {
  C, cfgNum, stateColor, stateLabel, fmt, fmtSigned,
  klineStatusMeta, feedStatusMeta, tsToClock,
  VERDICT_REF_LONG, VERDICT_REF_SHORT,
} from './types';
import type { HexpSnapshot, HexpConfig, KlineIngest } from './types';

interface Props {
  snap: HexpSnapshot | null;
  cfg: HexpConfig | null;
  /** K 线入库实时状态（按周期）；缺失时对应卡片不显示入库行 */
  klineStatus?: Record<string, KlineIngest>;
  /** 行情源（bridge 2s tick）最新更新距今秒数；null=未知 */
  feedAgeSec?: number | null;
}

const ResonancePanel: React.FC<Props> = ({ snap, cfg, klineStatus, feedAgeSec }) => {
  // 展示参考线（非交易阈值）：方案 B 已废除 verdict 硬封，此处仅用于滑杆分区着色
  const longTh = VERDICT_REF_LONG;
  const shortTh = VERDICT_REF_SHORT;
  const verdict = snap?.verdict ?? 0;
  const periods = snap?.used_periods?.length
    ? snap.used_periods
    : ['M5', 'H1', 'H4', 'D1'];
  const primary = snap?.primary_period ?? 'M5';

  /** verdict 归一到 0~100 的滑杆位置（-1 → 0%，+1 → 100%） */
  const pos = (v: number): number => Math.min(100, Math.max(0, ((v + 1) / 2) * 100));

  const verdictColor = verdict >= longTh ? C.up : verdict <= shortTh ? C.down : C.textDim;
  const verdictWord =
    verdict >= longTh ? '共振显著偏多' : verdict <= shortTh ? '共振显著偏空' : '共振不足 · 中性';

  // K 线入库汇总：统计各周期实时（live）数量
  const liveCount = periods.filter((p) => klineStatus?.[p]?.fresh === 'live').length;
  const ingestMeta =
    liveCount === periods.length && periods.length > 0
      ? { color: C.down, label: '实时' }
      : liveCount > 0
        ? { color: C.warn, label: '部分实时' }
        : { color: C.up, label: '异常' };
  const feed = feedStatusMeta(feedAgeSec ?? null);

  return (
    <Box
      sx={{
        p: 2, borderRadius: 2, background: C.panelBg,
        border: `1px solid ${C.border}`, height: '100%',
        display: 'flex', flexDirection: 'column',
      }}
    >
      <Typography sx={{ fontSize: 13, fontWeight: 700, color: C.textMain, mb: 0.6 }}>
        多周期共振矩阵
      </Typography>

      {/* K 线入库实时状态 · 汇总行 */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1.2, flexWrap: 'wrap' }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
          <Box
            sx={{
              width: 7, height: 7, borderRadius: '50%', background: feed.color,
              boxShadow: `0 0 6px ${feed.color}`,
            }}
          />
          <Typography sx={{ fontSize: 10, color: C.textDim }}>
            行情源 <b style={{ color: feed.color }}>{feed.label}</b>
            {feedAgeSec != null && Number.isFinite(feedAgeSec)
              ? ` · ${Math.round(feedAgeSec)}s前`
              : ''}
          </Typography>
        </Box>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
          <Box
            sx={{
              width: 7, height: 7, borderRadius: '50%', background: ingestMeta.color,
              boxShadow: `0 0 6px ${ingestMeta.color}`,
            }}
          />
          <Typography sx={{ fontSize: 10, color: C.textDim }}>
            K线入库 <b style={{ color: ingestMeta.color }}>{ingestMeta.label}</b>
            {` · ${liveCount}/${periods.length}`}
          </Typography>
        </Box>
      </Box>

      {/* 周期卡片 */}
      <Box sx={{ display: 'flex', gap: 1, mb: 2, flexWrap: 'wrap' }}>
        {periods.map((p) => {
          const st = (snap?.period_states ?? {})[p] ?? '';
          const ts = (snap?.trend_scores ?? {})[p];
          const w = cfgNum(cfg, `hexp.mtf.weight_${p}`);
          const sc = stateColor(st);
          const isPrimary = p === primary;
          return (
            <Tooltip
              key={p}
              arrow
              title={`${p} · ${stateLabel(st)} · TrendScore ${fmt(ts, 1)}${w ? ` · 共振权重 ${fmt(w, 2)}` : ''}`}
            >
              <Box
                sx={{
                  flex: '1 1 76px', minWidth: 76, p: 1, borderRadius: 1.5,
                  background: `${sc}14`,
                  border: `1px solid ${isPrimary ? sc : C.border}`,
                  boxShadow: isPrimary ? `0 0 0 1px ${sc}55 inset` : 'none',
                  position: 'relative',
                }}
              >
                {isPrimary && (
                  <Box
                    sx={{
                      position: 'absolute', top: 4, right: 5, fontSize: 8.5,
                      color: sc, fontWeight: 700, letterSpacing: '.04em',
                    }}
                  >
                    主
                  </Box>
                )}
                <Typography sx={{ fontSize: 12, fontWeight: 800, color: C.textMain }}>{p}</Typography>
                <Typography sx={{ fontSize: 10, color: sc, fontWeight: 600, mb: 0.5 }}>
                  {stateLabel(st)}
                </Typography>
                {/* TrendScore 条 */}
                <Box sx={{ height: 5, borderRadius: 3, background: 'rgba(148,163,184,0.14)', overflow: 'hidden' }}>
                  <Box
                    sx={{
                      height: '100%', borderRadius: 3,
                      width: `${Math.min(100, Math.max(0, ts ?? 0))}%`,
                      background: sc, transition: 'width .4s ease',
                    }}
                  />
                </Box>
                <Typography
                  sx={{ fontSize: 11, color: C.textDim, mt: 0.4, fontVariantNumeric: 'tabular-nums' }}
                >
                  {fmt(ts, 1)}
                  {w ? <span style={{ color: C.textFaint, fontSize: 9.5 }}> · w{fmt(w, 2)}</span> : null}
                </Typography>
                {/* K 线入库实时状态：最新棒时刻 + 新鲜度灯 */}
                {(() => {
                  const ks = klineStatus?.[p];
                  if (!ks) return null;
                  const m = klineStatusMeta(ks.fresh);
                  const tip =
                    `最新棒 ${tsToClock(ks.openTime)} · ${m.label}` +
                    (ks.tickCount !== null ? ` · 累计 ${ks.tickCount} ticks` : '') +
                    (ks.ageSec !== null ? ` · 距现在 ${Math.round(ks.ageSec)}s` : '');
                  return (
                    <Tooltip title={tip} arrow placement="top">
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.4, mt: 0.3, cursor: 'help' }}>
                        <Box
                          sx={{
                            width: 6, height: 6, borderRadius: '50%', flexShrink: 0,
                            background: m.color, boxShadow: `0 0 5px ${m.color}`,
                          }}
                        />
                        <Typography
                          sx={{ fontSize: 9, color: C.textFaint, fontVariantNumeric: 'tabular-nums' }}
                        >
                          K线 {tsToClock(ks.openTime)} · {m.label}
                          {ks.tickCount !== null ? ` ·${ks.tickCount}t` : ''}
                        </Typography>
                      </Box>
                    </Tooltip>
                  );
                })()}
              </Box>
            </Tooltip>
          );
        })}
      </Box>

      {/* 共振裁决滑杆 */}
      <Box sx={{ mt: 'auto' }}>
        <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1, mb: 0.8 }}>
          <Typography sx={{ fontSize: 11, color: C.textFaint }}>共振裁决 verdict</Typography>
          <Typography
            sx={{
              fontSize: 18, fontWeight: 800, color: verdictColor,
              fontVariantNumeric: 'tabular-nums', lineHeight: 1,
            }}
          >
            {fmtSigned(verdict, 3)}
          </Typography>
          <Typography sx={{ fontSize: 11, color: verdictColor, fontWeight: 600 }}>
            {verdictWord}
          </Typography>
        </Box>

        <Box sx={{ position: 'relative', height: 24 }}>
          {/* 轨道：空区 / 中性 / 多区 */}
          <Box sx={{ position: 'absolute', inset: '8px 0 auto 0', height: 8, borderRadius: 4, overflow: 'hidden', display: 'flex' }}>
            <Box sx={{ width: `${pos(shortTh)}%`, background: `${C.down}44` }} />
            <Box sx={{ width: `${pos(longTh) - pos(shortTh)}%`, background: 'rgba(148,163,184,0.16)' }} />
            <Box sx={{ width: `${100 - pos(longTh)}%`, background: `${C.up}44` }} />
          </Box>
          {/* 阈值刻度 */}
          {[
            { v: shortTh, c: C.down, t: `偏空参考线 ${fmt(shortTh, 2)}（视觉刻度，非硬封阈值）` },
            { v: longTh, c: C.up, t: `偏多参考线 ${fmt(longTh, 2)}（视觉刻度，非硬封阈值）` },
          ].map((m) => (
            <Tooltip key={m.t} title={m.t} arrow>
              <Box
                sx={{
                  position: 'absolute', top: 5, bottom: 5, width: 2,
                  left: `${pos(m.v)}%`, background: m.c, opacity: 0.9,
                }}
              />
            </Tooltip>
          ))}
          {/* 当前指针 */}
          <Box
            sx={{
              position: 'absolute', top: 2, left: `${pos(verdict)}%`,
              transform: 'translateX(-50%)',
              width: 3, height: 20, borderRadius: 1.5,
              background: verdictColor, boxShadow: `0 0 8px ${verdictColor}`,
              transition: 'left .4s ease',
            }}
          />
        </Box>
        <Box sx={{ display: 'flex', justifyContent: 'space-between' }}>
          <Typography sx={{ fontSize: 9.5, color: C.textFaint }}>-1 全空</Typography>
          <Typography sx={{ fontSize: 9.5, color: C.textFaint }}>0 中性</Typography>
          <Typography sx={{ fontSize: 9.5, color: C.textFaint }}>+1 全多</Typography>
        </Box>
      </Box>
    </Box>
  );
};

export default ResonancePanel;
