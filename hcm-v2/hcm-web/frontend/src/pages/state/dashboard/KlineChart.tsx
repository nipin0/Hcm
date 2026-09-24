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

  /* ── 轮次（rounds）【2026-09-17 D6】────────────────────────────────────────
   * 定义：**连续 `box_frozen=true` 的 bar 段 = 一轮**（进场后箱体被锁定，直到本轮结束）。
   *   ⇒ 段**起点 = 这轮在哪开的**（箱体锁定那一刻），**终点 = 本轮在哪结束/被破的**。
   * 为什么用冻结段而不是 S1 状态段：`box_frozen` 就是塔按"本轮已开仓"归一出来的语义
   *   标记（前端不必猜轮次边界）；S1 状态段会把"评估中、尚未开仓"的长段也染上色。
   * ⚠ 边界诚实性：颜色按**本视窗内**顺序循环取用、标签 `R1/R2…` 也是**本视窗内序号**
   *   （绝对轮号未落库）⇒ 颜色**只用于区分相邻轮次**，不代表全局第几轮。
   */
  const rounds = useMemo(() => {
    const FILL = ['#4ade80', '#60a5fa', '#f59e0b', '#a78bfa', '#22d3ee', '#fb7185'];
    const out: { from: number; to: number; color: string; label: string }[] = [];
    for (let i = 0; i < view.length; i++) {
      const b: any = view[i];
      const froz = b.box_frozen === true || b.box_frozen === 'true';
      if (!froz) continue;
      const last = out[out.length - 1];
      if (!last || last.to !== i - 1) {
        out.push({ from: i, to: i, color: FILL[out.length % FILL.length], label: '' });
      } else {
        last.to = i;
      }
    }
    out.forEach((r, k) => { r.label = `R${k + 1}`; });
    return out;
  }, [view]);

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

    /* ── 箱体【2026-09-17 D5】"形态直观"四件套（数据源不变，只改呈现）───────────
     * 数据源：`/kline` 每根 bar 的 `box_upper/box_lower/box_mid/box_frozen`
     *   （塔逐 bar 落 `intent.box_*`；`state_strategy` 已按"冻结箱/滚动箱"归一
     *    ⇒ 前端**不自行判断口径**，只负责画）。
     * 本次四件套（解决"三条线看不出形态"）：
     *   ① **填充带**：`[下沿, 上沿]` 加极淡底色（滚动 6% / 冻结 10%）⇒ 箱体是**区域**
     *      而非三条线；破界/断线处缺口一眼可见（用 stack 两段画带：`下沿` + `上沿-下沿`）。
     *   ② **主次线型**：上/下沿 = 实线（边界）；中轨 = **点线**且更细（辅助线，止盈锚点）。
     *   ③ **右侧数值标签**：每条线在"最后一个有值的 bar"打出 `顶/中/底 xxxx`（仅当该值
     *      在最后 3 根内 —— 否则是历史段，不打标签以免图中杂乱）。
     *   ④ **现价箱内位置条**（DOM，见下方 strip）：`箱内 62%（中轨上方 +2.1 点）`
     *      或 `已破箱顶 +0.8 点` —— 直接回答"价格在箱体哪里"（此前被误读的那一点）。
     * 颜色纪律：箱体恒用 `BOX_COLOR`（**需求方指定**），冻结段只用**灰+虚线**区分线型，
     *   不换色（避免与"箱体绿"打架）；null 一律**断线**（不补、不外推）。
     * ⚠ 历史轮次的冻结箱不可复原（回填=滚动近似，`box_frozen` 留空）；塔实时写入为
     *   冻结感知并覆盖同 bar ⇒ 越新越权威。
     */
    const ctx = live?.ctx || {};
    const dv = live?.derived;
    const lastIdx = Math.max(0, view.length - 1);
    const okOf = (k: string, b: any) => {
      const v = Number(b ? b[k] : NaN);
      return Number.isFinite(v) && v > 0;
    };
    const isFroz = (b: any) => b?.box_frozen === true || b?.box_frozen === 'true';
    const nBox = view.reduce((n, b: any) => n + (okOf('box_upper', b) ? 1 : 0), 0);

    const BOX_LEVELS = [
      { tag: '箱顶', short: '顶', key: 'box_upper' },
      { tag: '中轨', short: '中', key: 'box_mid' },
      { tag: '箱底', short: '底', key: 'box_lower' },
    ] as const;
    const boxBands: any[] = [];
    const boxSeries: any[] = [];
    /** 填充带：stack 两段（`下沿` + `上沿-下沿`）⇒ 覆盖 [下沿, 上沿]。
     *  两条序列**必须同 null 模式**：否则 ECharts 的 stack 会把缺失点当 0，
     *  填充从图底拉起并把 y 轴拉爆（D5 已按此口径验证）。 */
    const pushBand = (mask: boolean[], key: string, color: string, opacity: number) => {
      const seg = view.map((b: any, i: number) =>
        (mask[i] && okOf('box_lower', b) && okOf('box_upper', b))
          ? [Number(b.box_lower), Number(b.box_upper) - Number(b.box_lower)]
          : null);
      const sid = `boxband_${key}`;
      boxBands.push({
        type: 'line', name: `箱带·${key}`, stack: sid,
        silent: true, showSymbol: false, connectNulls: false, z: 0,
        data: seg.map((v) => (v ? v[0] : null)),
        lineStyle: { opacity: 0 }, areaStyle: { opacity: 0 },
      });
      boxBands.push({
        type: 'line', name: `箱带·${key}·填充`, stack: sid,
        silent: true, showSymbol: false, connectNulls: false, z: 0,
        data: seg.map((v) => (v ? v[1] : null)),
        lineStyle: { opacity: 0 },
        areaStyle: { color, opacity },
      });
    };
    // ① 填充带：滚动段统一箱体绿；**冻结段按轮次换底色** ⇒ 轮界一眼可见
    pushBand(view.map((b: any) => !isFroz(b)), '滚动', BOX_COLOR, 0.06);
    rounds.forEach((r, k) => {
      pushBand(view.map((_, i) => i >= r.from && i <= r.to), `R${k + 1}`, r.color, 0.13);
    });
    // ②③ 三条线：滚动 = 实线（中轨点线）；冻结 = 灰虚线（**线色线型不随轮次变**，
    //   轮次信息只落在底色上 ⇒ 边界线本身保持稳定可读）。右端打 顶/中/底 数值标签。
    BOX_LEVELS.forEach((lv) => {
      (['roll', 'froz'] as const).forEach((kind) => {
        const frozenSeg = kind === 'froz';
        const lineColor = frozenSeg ? '#94a3b8' : BOX_COLOR;
        const mine = view.map((b: any) =>
          (okOf(lv.key, b) && isFroz(b) === frozenSeg ? Number(b[lv.key]) : null));
        let lastWith = -1;
        mine.forEach((v, i) => { if (v !== null) lastWith = i; });
        const recent = lastWith >= view.length - 3;   // 只在"贴近当前"时打标签
        boxSeries.push({
          type: 'line', name: `箱体${lv.tag}${frozenSeg ? '·冻结' : '·滚动'}`,
          silent: true, showSymbol: false, connectNulls: false, z: 1,
          data: mine.map((v, i) => (v === null ? null
            : ((recent && i === lastWith)
              ? {
                value: v,
                label: {
                  show: true, position: 'right', distance: 3,
                  formatter: `${lv.short} ${v.toFixed(1)}`,
                  color: lineColor, fontSize: 9, fontWeight: 'bold' as const,
                },
              }
              : v))),
          label: { show: false },
          lineStyle: {
            color: lineColor,
            type: frozenSeg ? 'dashed' : (lv.key === 'box_mid' ? 'dotted' : 'solid'),
            width: lv.key === 'box_mid' ? 1.0 : (frozenSeg ? 1.4 : 1.3),
          },
        });
      });
    });
    // 轮次分隔线：在本轮**开仓/锁定那一刻**画竖虚线 + `R1/R2…` 标签
    const roundMarkLines = rounds.map((r) => ({
      xAxis: r.from,
      label: {
        show: true, formatter: r.label, position: 'insideEndTop' as const,
        color: r.color, fontSize: 9, fontWeight: 'bold' as const,
      },
      lineStyle: { color: r.color, type: 'dashed' as const, width: 1, opacity: 0.55 },
    }));

    // ④ 现价箱内位置：DOM 位置条用（在组件体内计算，见下方 `stripInfo`）
    const boxNote = nBox === 0
      ? '箱体：该区间无逐 bar 箱体数据（塔自 2026-09-17 起逐 bar 落库）'
      : `箱体（逐 bar 真实值 · ${nBox}/${view.length} 根）`
        + `｜填充=箱体区间${rounds.length
          ? `（冻结段=本轮已开仓，按轮换底色 · 本视窗 ${rounds.length} 轮）`
          : '（本视窗无冻结轮）'}`
        + '｜实线=滚动箱｜灰虚线=冻结箱｜点线=中轨';

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
      // 【2026-09-17 修复 D2】把箱体口径直接写在图上，消除"当前值被读成历史箱体"的误判
      title: {
        text: boxNote, left: 60, top: 0,
        textStyle: { color: C.weak, fontSize: 10, fontWeight: 'normal' },
      },
      // 【2026-09-17 D6】右侧留白 1cm（@96dpi：1cm ≈ 37.8px ⇒ 62 → 100）。
      //   此前图表紧贴右边缘，箱体右端数值标签几乎顶框；左缩 1cm 后留白稳定、标签有余量。
      grid: { left: 58, right: 62 + Math.round(1 * 37.8), top: 12, bottom: 44 },
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
            + row('state（执行态·下单依据）', `<span style="color:${sc}">${b.state || '—'}（${STATE_CN[b.state || ''] || '-'}）</span>`)
            + row('predicted_class（模型·仅参考）', b.predicted_class ? (CLASS_CN[b.predicted_class] || b.predicted_class) : '—')
            + row('proba', [b.prob_oscillation, b.prob_trend_init, b.prob_trend_mid, b.prob_trend_fade]
                .map((x) => fmt(x, 3)).join(' / '))
            + row('margin', fmt(b.margin, 3))
            + row('direction', b.direction || '—')
            + row('age_bars', String(b.age_bars ?? '—'))
            + row('transitioned', String(b.transitioned ?? '—'))
            + row('note', b.note || '—')
            // 【2026-09-17 D4】显示**本 bar 自己的**箱体（逐 bar 真值），不再贴"当前值"。
            + row('箱体 上/中/下（本 bar）', Number.isFinite(Number((b as any).box_upper))
                ? `${fmt((b as any).box_upper)} / ${fmt((b as any).box_mid)} / ${fmt((b as any).box_lower)}`
                  + `${(b as any).box_frozen === true ? '　〔冻结箱·本轮锁定〕' : '　〔滚动箱〕'}`
                : `<i style="color:${C.block}">本 bar 无箱体数据</i>`)
            // 【2026-09-17 D5】本 bar 收盘价在**本 bar 自己的箱体**里的位置（直观回答"在箱底还是箱顶"）
            + row('箱内位置（本 bar 收盘）', (() => {
                const up = Number((b as any).box_upper);
                const lo = Number((b as any).box_lower);
                const mi = Number((b as any).box_mid);
                const c = Number(b.close);
                if (!(up > lo) || !Number.isFinite(c)) {
                  return `<i style="color:${C.weak}">本 bar 无箱体数据</i>`;
                }
                if (c > up) return `<span style="color:${C.block}">已破箱顶 +${(c - up).toFixed(2)} 点</span>`;
                if (c < lo) return `<span style="color:${C.block}">已破箱底 ${(c - lo).toFixed(2)} 点</span>`;
                return `${(((c - lo) / (up - lo)) * 100).toFixed(0)}%`
                  + `（中轨${c >= mi ? '上方' : '下方'}）`;
              })())
            // 【2026-09-17 D6】轮次：本 bar 是否落在某轮冻结段（= 本轮已开仓、箱体锁定）
            + row('轮次', (() => {
                const k = rounds.findIndex((r) => view.indexOf(b) >= r.from && view.indexOf(b) <= r.to);
                if (k < 0) return `<i style="color:${C.weak}">未开仓（滚动箱，每 bar 重算）</i>`;
                const r = rounds[k];
                return `<span style="color:${r.color};font-weight:700">${r.label}</span>`
                  + `（${tsShort((view[r.from] as any).open_time).slice(6)} 开 · 冻结 `
                  + `${r.to - r.from + 1} 根）`;
              })())
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
          markPoint: { data: marks, silent: true },
          // 【2026-09-17 D6】轮次分隔：在本轮"开仓/锁箱"那一刻画竖虚线 + R1/R2… 标签
          markLine: { silent: true, symbol: 'none', data: roundMarkLines },
        },
        // 【2026-09-17 D5】箱体画法：填充带（z=0，最底层，不遮蜡烛）→ 三条线（z=1）
        ...boxBands,
        ...boxSeries,
      ],
    };
  }, [view, live, orders, positions]);

  /* 【2026-09-17 D5】箱体位置条（DOM，非 canvas）——"价格在箱体哪里"一眼可见。
   * 用**最后一个有箱体值的 bar** 的箱体（= 最贴近当前的断言）对比最新收盘价；
   * 破界时直接写"已破箱顶/箱底 ±x 点"，不再让人自己数格子（那正是前两次误读的根源）。 */
  const strip = useMemo(() => {
    let info: { t: string; froz: boolean; up: number; mid: number; lo: number } | null = null;
    for (let i = view.length - 1; i >= 0; i--) {
      const b: any = view[i];
      const up = Number(b ? b.box_upper : NaN);
      const lo = Number(b ? b.box_lower : NaN);
      if (Number.isFinite(up) && up > 0 && Number.isFinite(lo) && lo > 0) {
        info = {
          t: b.open_time,
          froz: b.box_frozen === true || b.box_frozen === 'true',
          up, mid: Number(b.box_mid), lo,
        };
        break;
      }
    }
    const close = Number(view[view.length - 1] ? view[view.length - 1].close : NaN);
    let text = '无箱体数据';
    let color: string = C.weak;
    if (info && Number.isFinite(close) && info.up > info.lo) {
      if (close > info.up) {
        text = `已破箱顶 +${(close - info.up).toFixed(2)} 点`; color = C.block;
      } else if (close < info.lo) {
        text = `已破箱底 ${(close - info.lo).toFixed(2)} 点`; color = C.block;
      } else {
        const pct = ((close - info.lo) / (info.up - info.lo)) * 100;
        const d = close - info.mid;
        text = `箱内 ${pct.toFixed(0)}%（中轨${d >= 0 ? '上方 +' : '下方 '}${d.toFixed(2)} 点）`;
        color = (pct >= 85 || pct <= 15) ? C.warn : C.ok;
      }
    }
    return { info, close, text, color };
  }, [view]);

  const stripItem = (k: string, v: React.ReactNode, color?: string, bold?: boolean) => (
    <span style={{ color: color || C.weak, fontWeight: bold ? 700 : 400 }}>
      <span style={{ color: C.weak }}>{k}</span> {v}
    </span>
  );

  return (
    <div>
      <div style={{
        display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center',
        fontSize: 11, padding: '2px 2px 5px', fontVariantNumeric: 'tabular-nums',
      }}>
        {stripItem('箱体', strip.info
          ? `${strip.info.froz ? '冻结箱·本轮锁定' : '滚动箱'} · ${tsShort(strip.info.t).slice(6)} bar`
          : '无数据', C.sub)}
        {stripItem('顶', strip.info ? fmt(strip.info.up) : '—', BOX_COLOR, true)}
        {stripItem('中', strip.info ? fmt(strip.info.mid) : '—', BOX_COLOR)}
        {stripItem('底', strip.info ? fmt(strip.info.lo) : '—', BOX_COLOR, true)}
        {stripItem(`现价 ${Number.isFinite(strip.close) ? fmt(strip.close) : '—'} ·`,
          strip.text, strip.color, true)}
        {/* 【2026-09-17 D6】当前轮：末根附近若处冻结段 ⇒ 就是"正在进行的这一轮" */}
        {rounds.length > 0 ? (() => {
          const k = (() => {
            for (let i = rounds.length - 1; i >= 0; i--) {
              if (rounds[i].to >= view.length - 3) return i;
            }
            return rounds.length - 1;
          })();
          const r = rounds[k];
          return stripItem(`${r.label} 轮`,
            `${tsShort((view[r.from] as any).open_time).slice(6)} 开 · 冻结 ${r.to - r.from + 1} 根`
            + `（本视窗共 ${rounds.length} 轮，按轮换底色）`, r.color, true);
        })() : stripItem('轮次', '本视窗无冻结轮（尚未开仓）', C.weak)}
        <span style={{ color: C.weak }}>
          填充=箱体区间（冻结段按轮换色）｜实线=滚动箱｜灰虚线=冻结箱｜点线=中轨
        </span>
      </div>
      <ReactEChartsCore echarts={echarts} option={option} style={{ height: 320 }}
                        notMerge lazyUpdate />
    </div>
  );
}
