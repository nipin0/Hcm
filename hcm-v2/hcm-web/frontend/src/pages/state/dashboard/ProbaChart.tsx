import React, { useMemo } from 'react';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import echarts from './echartsSetup';
import { C, STATE_COLOR, CLASS_KEYS, CLASS_CN, CLASS_LINE_COLOR, fmt, tsShort } from './theme';
import type { KlineBar } from './theme';

interface Props {
  bars: KlineBar[];
  cursor: number;
}

export default function ProbaChart({ bars, cursor }: Props) {
  const view = useMemo(() => bars.slice(0, cursor + 1), [bars, cursor]);

  const option = useMemo(() => {
    const times = view.map((b) => tsShort(b.open_time).slice(6));

    /* 状态切换节点（transitioned=true）→ 竖向虚线 */
    const transitions = view
      .map((b, i) => (b.transitioned ? i : -1))
      .filter((i) => i >= 0);

    /* 状态背景带（挂在第一条曲线上） */
    const areas: any[] = [];
    let segStart = 0;
    for (let i = 1; i <= view.length; i++) {
      const cur = view[i]?.state;
      const prev = view[segStart]?.state;
      if (i === view.length || cur !== prev) {
        if (prev) {
          areas.push([
            { xAxis: segStart, itemStyle: { color: `${STATE_COLOR[prev] || C.idle}14` } },
            { xAxis: i - 1 },
          ]);
        }
        segStart = i;
      }
    }

    return {
      animation: false,
      backgroundColor: 'transparent',
      grid: { left: 46, right: 16, top: 26, bottom: 26 },
      legend: {
        top: 0, right: 8, itemWidth: 12, itemHeight: 6, itemGap: 12,
        textStyle: { color: C.sub, fontSize: 10.5 },
        data: CLASS_KEYS.map((k) => CLASS_CN[k]),
      },
      xAxis: {
        type: 'category',
        data: times,
        boundaryGap: false,
        axisLine: { lineStyle: { color: C.border } },
        axisLabel: { color: C.weak, fontSize: 10, interval: Math.max(1, Math.floor(times.length / 9)) },
      },
      yAxis: {
        min: 0, max: 1,
        splitLine: { lineStyle: { color: C.divider } },
        axisLabel: { color: C.weak, fontSize: 10 },
      },
      tooltip: {
        trigger: 'axis',
        backgroundColor: '#0b0f1aee',
        borderColor: '#334155',
        textStyle: { color: C.text, fontSize: 11 },
        formatter: (ps: any[]) => {
          if (!ps?.length) return '';
          const b: KlineBar = view[ps[0].dataIndex];
          if (!b) return '';
          const sc = STATE_COLOR[b.state || ''] || C.idle;
          return (
            `<div style="font-weight:700;color:#93c5fd;margin-bottom:4px">${tsShort(b.open_time)}</div>`
            + `<div style="color:${sc};margin-bottom:4px">${b.state || '—'}`
            + `${b.transitioned ? ' · <b>状态切换</b>' : ''}</div>`
            + CLASS_KEYS.map((k) => {
                const v = (b as any)[`prob_${k}`];
                return `<div><span style="color:${CLASS_LINE_COLOR[k]}">● </span>`
                     + `<span style="color:${C.weak}">${CLASS_CN[k]}</span> ${fmt(v, 3)}</div>`;
              }).join('')
            + `<div style="margin-top:4px"><span style="color:${C.weak}">margin </span>${fmt(b.margin, 3)}</div>`
          );
        },
      },
      series: CLASS_KEYS.map((k, idx) => ({
        name: CLASS_CN[k],
        type: 'line',
        smooth: false,
        showSymbol: false,
        lineStyle: { width: 1.6, color: CLASS_LINE_COLOR[k] },
        itemStyle: { color: CLASS_LINE_COLOR[k] },
        emphasis: { focus: 'series' as const },
        data: view.map((b) => {
          const v = (b as any)[`prob_${k}`];
          return v == null ? null : Number(v);
        }),
        ...(idx === 0
          ? {
              markArea: { silent: true, data: areas },
              markLine: {
                silent: true,
                symbol: 'none',
                lineStyle: { color: '#3b82f6aa', type: 'dashed', width: 1 },
                label: { show: false },
                data: transitions.map((i) => ({ xAxis: i })),
              },
            }
          : {}),
      })),
    };
  }, [view]);

  return <ReactEChartsCore echarts={echarts} option={option} style={{ height: 230 }}
                           notMerge lazyUpdate />;
}
