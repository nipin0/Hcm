import React, { useMemo } from 'react';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import echarts from './echartsSetup';
import { C, STATE_COLOR, STATE_CN, CLASS_CN, BOX_COLOR, UD, fmt, tsShort } from './theme';
import type { KlineBar, LiveResp } from './theme';

interface Props {
  bars: KlineBar[];
  cursor: number;
  live: LiveResp | null;
  orders: any[];
  positions: any[];
}

/** 按时间就近匹配 bar 下标（后端无 signal_id 关联，只能近似 —— 已在 UI 标注） */
const nearestIdx = (bars: KlineBar[], iso?: string | null): number => {
  if (!iso || !bars.length) return -1;
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return -1;
  let best = -1;
  let bd = Infinity;
  for (let i = 0; i < bars.length; i++) {
    const d = Math.abs(new Date(bars[i].open_time).getTime() - t);
    if (d < bd) { bd = d; best = i; }
  }
  return best;
};

export default function KlineChart({ bars, cursor, live, orders, positions }: Props) {
  const view = useMemo(() => bars.slice(0, cursor + 1), [bars, cursor]);

  const option = useMemo(() => {
    const times = view.map((b) => tsShort(b.open_time).slice(6));
    const ohlc = view.map((b) => [Number(b.open), Number(b.close), Number(b.low), Number(b.high)]);

    /* 行情状态背景色块：连续同状态合并成一段 */
    const areas: any[] = [];
    let segStart = 0;
    for (let i = 1; i <= view.length; i++) {
      const cur = view[i]?.state;
      const prev = view[segStart]?.state;
      if (i === view.length || cur !== prev) {
        if (prev) {
          areas.push([
            { xAxis: segStart, itemStyle: { color: `${STATE_COLOR[prev] || C.idle}1f` } },
            { xAxis: i - 1 },
          ]);
        }
        segStart = i;
      }
    }

    /* 箱体三线：PG 未落历史箱体 → 只能画当前值（在 UI 注明），非震荡态用灰虚线 */
    const ctx = live?.ctx || {};
    const dv = live?.derived;
    const frozen = !!ctx.box_frozen;
    const boxColor = frozen ? '#64748b' : BOX_COLOR;
    const boxLines = [
      { yAxis: ctx.box_upper, tag: '上沿' },
      { yAxis: ctx.box_mid, tag: '中轨' },
      { yAxis: ctx.box_lower, tag: '下沿' },
    ]
      .filter((x) => Number.isFinite(Number(x.yAxis)))
      .map((x) => ({
        yAxis: Number(x.yAxis),
        lineStyle: { color: boxColor, type: frozen ? 'dashed' : 'solid', width: 1.2 },
        label: {
          formatter: `${x.tag} ${fmt(x.yAxis)}`,
          color: boxColor,
          fontSize: 10,
          position: 'insideEndTop',
        },
      }));

    /* 开平仓标记：按时间就近吸附到 bar（后端无 signal_id，属近似） */
    const marks: any[] = [];
    (positions || []).forEach((p) => {
      const i = nearestIdx(view, p.open_time);
      if (i < 0) return;
      marks.push({
        name: '开仓',
        coord: [i, Number(view[i].low) - 1],
        value: '开',
        symbol: 'triangle',
        symbolSize: 11,
        itemStyle: { color: String(p.direction).toUpperCase() === 'BUY' ? UD.long : UD.short },
        label: { show: false },
      });
    });
    (orders || []).forEach((o) => {
      const i = nearestIdx(view, o.open_time);
      if (i >= 0) {
        marks.push({
          name: '开仓', coord: [i, Number(view[i].low) - 1], value: '开', symbol: 'triangle',
          symbolSize: 11,
          itemStyle: { color: String(o.direction).toUpperCase() === 'BUY' ? UD.long : UD.short },
          label: { show: false },
        });
      }
      const j = nearestIdx(view, o.close_time);
      if (j >= 0) {
        marks.push({
          name: '平仓', coord: [j, Number(view[j].high) + 1], value: '平', symbol: 'triangle',
          symbolSize: 11, symbolRotate: 180,
          itemStyle: { color: String(o.direction).toUpperCase() === 'BUY' ? UD.long : UD.short },
          label: { show: false },
        });
      }
    });

    return {
      animation: false,
      backgroundColor: 'transparent',
      grid: { left: 58, right: 62, top: 12, bottom: 44 },
      legend: { show: false },
      xAxis: {
        type: 'category',
        data: times,
        boundaryGap: true,
        axisLine: { lineStyle: { color: C.border } },
        axisLabel: { color: C.weak, fontSize: 10, interval: Math.max(1, Math.floor(times.length / 9)) },
        splitLine: { show: false },
      },
      yAxis: {
        scale: true,
        splitLine: { lineStyle: { color: C.divider } },
        axisLabel: { color: C.weak, fontSize: 10, formatter: (v: number) => Number(v).toFixed(1) },
      },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross', crossStyle: { color: C.weak }, label: { backgroundColor: '#334155' } },
        backgroundColor: '#0b0f1aee',
        borderColor: '#334155',
        textStyle: { color: C.text, fontSize: 11 },
        formatter: (ps: any[]) => {
          const p = Array.isArray(ps) ? ps[0] : ps;
          const b: KlineBar = view[p.dataIndex];
          if (!b) return '';
          const sc = STATE_COLOR[b.state || ''] || C.idle;
          const od = orders.find((o) => nearestIdx([b], o.open_time) === 0 && !!o.open_time);
          const row = (k: string, v: React.ReactNode) =>
            `<tr><td style="color:${C.weak};padding-right:10px">${k}</td><td>${v}</td></tr>`;
          return (
            `<div style="font-weight:700;color:#93c5fd;margin-bottom:4px">${tsShort(b.open_time)} UTC`
            + `${od ? ` · 本 bar 附近有开仓` : ''}</div><table style="border-collapse:collapse">`
            + row('OHLC', `${fmt(b.open)} / ${fmt(b.high)} / ${fmt(b.low)} / ${fmt(b.close)}`)
            + row('state', `<span style="color:${sc}">${b.state || '—'}（${STATE_CN[b.state || ''] || '-'}）</span>`)
            + row('predicted_class', b.predicted_class ? (CLASS_CN[b.predicted_class] || b.predicted_class) : '—')
            + row('proba', [b.prob_oscillation, b.prob_trend_init, b.prob_trend_mid, b.prob_trend_fade]
                .map((x) => fmt(x, 3)).join(' / '))
            + row('margin', fmt(b.margin, 3))
            + row('direction', b.direction || '—')
            + row('age_bars', String(b.age_bars ?? '—'))
            + row('transitioned', String(b.transitioned ?? '—'))
            + row('note', b.note || '—')
            + row('box_upper/lower', b.state === 'S1_OSC'
                ? `${fmt(ctx.box_upper)} / ${fmt(ctx.box_lower)}`
                : `<i style="color:${C.block}">非震荡态 → 箱体冻结</i>`)
            + row('27 维特征', `<i style="color:${C.block}">需 P1 落库</i>`)
            + `</table>`
          );
        },
      },
      dataZoom: [
        { type: 'inside', xAxisIndex: 0, minValueSpan: 20 },
        {
          type: 'slider', xAxisIndex: 0, height: 15, bottom: 6,
          borderColor: C.border, backgroundColor: C.inner,
          fillerColor: '#3b82f633', handleStyle: { color: C.info },
          textStyle: { color: C.weak, fontSize: 9 },
        },
      ],
      series: [
        {
          type: 'candlestick',
          name: 'XAUUSD',
          data: ohlc,
          itemStyle: {
            color: UD.up, color0: UD.down,
            borderColor: UD.up, borderColor0: UD.down,
          },
          markArea: { silent: true, data: areas },
          markLine: { silent: true, symbol: 'none', data: boxLines },
          markPoint: { data: marks, silent: true },
        },
      ],
    };
  }, [view, live, orders, positions]);

  return <ReactEChartsCore echarts={echarts} option={option} style={{ height: 320 }}
                           notMerge lazyUpdate />;
}
