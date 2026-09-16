import React from 'react';
import { Box, Chip, Paper, Typography } from '@mui/material';
import {
  C, STATE_COLOR, STATE_CN, CLASS_CN, CLASS_KEYS, CLASS_LINE_COLOR, BOX_COLOR, UD,
  fmt, fmtSigned, tsShort,
} from './theme';
import type { LiveResp, RiskResp } from './theme';

/* ── 通用小件 ───────────────────────────────────────────────────────────── */
export const KV = ({ k, v }: { k: string; v: React.ReactNode }) => (
  <Box sx={{
    display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', gap: 1, py: 0.55,
    borderBottom: `1px dashed ${C.divider}`,
    '&:last-of-type': { borderBottom: 0 },
  }}>
    <Box sx={{ color: C.weak, fontSize: 11.5, whiteSpace: 'nowrap' }}>{k}</Box>
    <Box sx={{ color: C.text, fontSize: 12, fontWeight: 600, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}>
      {v}
    </Box>
  </Box>
);

/** 「无数据源」标记 —— 用于如实标注后端确实没有的字段，**不伪造 0** */
export const NoSrc = ({ reason }: { reason?: string }) => (
  <Box component="span" sx={{ color: C.block, fontStyle: 'italic', fontWeight: 500 }}
       title={reason || '该字段当前无线上数据源'}>
    无数据源
  </Box>
);

const Card = ({ title, badge, right, children }: {
  title: string; badge?: React.ReactNode; right?: React.ReactNode; children: React.ReactNode;
}) => (
  <Paper elevation={0} sx={{ backgroundColor: C.card, border: `1px solid ${C.border}`, borderRadius: 2, p: 2 }}>
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.8, mb: 1.2 }}>
      <Typography sx={{ color: C.text, fontSize: 13, fontWeight: 600 }}>{title}</Typography>
      {badge}
      {right && <Box sx={{ ml: 'auto', color: C.weak, fontSize: 11 }}>{right}</Box>}
    </Box>
    {children}
  </Paper>
);

const SrcPill = ({ ok }: { ok: 'full' | 'partial' | 'none' }) => {
  const map = {
    full: { t: '有数据源', c: '#4ade80', b: '#2a5f3f' },
    partial: { t: '部分缺', c: '#fbbf24', b: '#6b5416' },
    none: { t: '无数据源', c: '#f87171', b: '#6b2424' },
  }[ok];
  return (
    <Chip label={map.t} size="small"
          sx={{ height: 17, fontSize: 9.5, color: map.c, backgroundColor: 'transparent',
                border: `1px solid ${map.b}`, '& .MuiChip-label': { px: 0.7 } }} />
  );
};

const StateBadge = ({ state }: { state?: string | null }) => {
  const s = state || '';
  const col = STATE_COLOR[s] || C.idle;
  return (
    <Box sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.8, px: 1.2, py: 0.4,
               borderRadius: 1.2, border: `1px solid ${col}66`, backgroundColor: `${col}22` }}>
      <Box sx={{ width: 9, height: 9, borderRadius: 0.5, backgroundColor: col }} />
      <Box sx={{ color: col, fontWeight: 700, fontSize: 13.5, letterSpacing: 0.2 }}>{s || '—'}</Box>
    </Box>
  );
};

/* ── 卡片 1：FSM 状态 ───────────────────────────────────────────────────── */
/* 数据源：hcm:state:fsm（防抖计数）+ hcm:live:state（行情形态） */
export const FsmCard = ({ live }: { live: LiveResp | null }) => {
  const fsm = live?.fsm || {};
  const lv = live?.live || {};
  const dv = live?.derived;
  const state = dv?.state ?? null;
  const pc = lv.predicted_class ?? null;
  return (
    <Card title="FSM 状态" badge={<SrcPill ok="full" />}
          right={dv ? STATE_CN[state || ''] || '' : ''}>
      <Box sx={{ mb: 1 }}><StateBadge state={state} /></Box>
      <KV k="行情形态 predicted_class" v={pc ? `${CLASS_CN[pc] || pc}` : '—'} />
      <KV k="防抖计数 pending_streak"
          v={Number(fsm.pending_streak) > 0
            ? <Box component="span" sx={{ color: C.warn }}>{fsm.pending_streak}</Box>
            : 0} />
      <KV k="pending_class" v={fsm.pending_class ? (CLASS_CN[fsm.pending_class] || fsm.pending_class) : '—'} />
      <KV k="age_bars（状态持续根数）" v={fsm.age_bars ?? '—'} />
      <KV k="hold_only（仅持有）"
          v={fsm.hold_only
            ? <Box component="span" sx={{ color: C.warn }}>true</Box>
            : 'false'} />
      <KV k="prev_state" v={lv.prev_state || '—'} />
      <KV k="note" v={lv.note || '—'} />
      <KV k="infer_ok / reason"
          v={<>{lv.infer_ok === false
            ? <Box component="span" sx={{ color: C.block }}>false</Box>
            : <Box component="span" sx={{ color: C.ok }}>true</Box>} / {lv.infer_reason || '—'}</>} />
      <KV k="model_version" v={lv.model_version || '—'} />
      <KV k="稳定行情态" v={<NoSrc reason="state_machine.py 无该字段；可用 hold_only + age_bars 替代" />} />
    </Card>
  );
};

/* ── 卡片 2：趋势方向 ───────────────────────────────────────────────────── */
/* 数据源：仅 direction 有（fsm 快照）；斜率/+DI/-DI/方向防抖计数 均无线上发布 */
export const TrendCard = ({ live }: { live: LiveResp | null }) => {
  const fsm = live?.fsm || {};
  const dv = live?.derived;
  const d = (fsm.direction || 'none').toLowerCase();
  const col = d === 'up' ? UD.up : d === 'down' ? UD.down : C.weak;
  return (
    <Card title="趋势方向" badge={<SrcPill ok="partial" />}>
      <Box sx={{ color: col, fontWeight: 700, fontSize: 19, mb: 1, letterSpacing: 0.5 }}>
        {d.toUpperCase()}
      </Box>
      <KV k="direction（fsm 快照）" v={fsm.direction || '—'} />
      <KV k="斜率 slope_atr" v={<NoSrc reason={dv?.trend_detail_reason} />} />
      <KV k="+DI" v={<NoSrc reason={dv?.trend_detail_reason} />} />
      <KV k="−DI" v={<NoSrc reason={dv?.trend_detail_reason} />} />
      <KV k="di_spread" v={<NoSrc reason={dv?.trend_detail_reason} />} />
      <KV k="方向防抖计数" v={<NoSrc reason="trend_direction.py 为纯窗口函数 debounce_bars，无计数器变量" />} />
    </Card>
  );
};

/* ── 卡片 3：箱体状态 ───────────────────────────────────────────────────── */
export const BoxCard = ({ live }: { live: LiveResp | null }) => {
  const ctx = live?.ctx || {};
  const dv = live?.derived;
  const cfg = live?.config || {};
  const frozen = !!ctx.box_frozen;
  const isOsc = !!dv?.is_osc;
  const mis = !!dv?.freeze_rule_misaligned;
  return (
    <Card title="箱体状态" badge={<SrcPill ok="full" />}
          right={isOsc ? '震荡态 → 箱体有效' : '非 S1_OSC 震荡态'}>
      {/* 启用/冻结：如实显示后端字段；UI 的"非 S1 置灰"是视觉规则，两者不一致时如实并列 */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.8, mb: 1, flexWrap: 'wrap' }}>
        <Chip size="small" label={frozen ? '冻结' : '启用'}
              sx={{ height: 20, fontSize: 11, fontWeight: 600,
                    color: frozen ? '#cbd5e1' : '#4ade80',
                    backgroundColor: frozen ? '#3a3a3a' : '#0e2418' }} />
        {mis && (
          // 常态差异（非 S1 且 box_frozen=false 是当前线上常见组合），用弱色通报而非告警，
          // 避免面板长期挂一个黄色警告造成"狼来了"。字段本身照常如实展示。
          <Box sx={{ color: C.weak, fontSize: 10 }} title="面板置灰是 UI 规则（非 S1 即置灰）；后端 box_frozen 字段当前为假，二者不同源，此处如实并列">
            ℹ 置灰规则与 box_frozen 字段不同源
          </Box>
        )}
      </Box>
      <KV k="上沿 box_upper" v={fmt(ctx.box_upper)} />
      <KV k="下沿 box_lower" v={fmt(ctx.box_lower)} />
      <KV k="中轨 box_mid" v={fmt(ctx.box_mid)} />
      <KV k="箱体高度（自算 upper−lower）"
          v={<Box component="span" sx={{ color: BOX_COLOR }}>{fmt(dv?.box_height)}</Box>} />
      <KV k={`最小箱体高度阈值（×ATR）`}
          v={cfg.box_min_width_atr ? fmt(cfg.box_min_width_atr.value, 2) : '—'} />
      <KV k="box_frozen_at" v={ctx.box_frozen_at ? tsShort(ctx.box_frozen_at) : '—'} />
      <KV k="consec_losses / add_count" v={`${ctx.consec_losses ?? '—'} / ${ctx.add_count ?? '—'}`} />
    </Card>
  );
};

/* ── 卡片 4：持仓风控 ───────────────────────────────────────────────────── */
export const RiskCard = ({ live, risk }: { live: LiveResp | null; risk: RiskResp | null }) => {
  const dir = live?.directive || {};
  const pos = risk?.positions?.[0];
  const fp = risk?.float_profit_sum ?? 0;
  const dl = risk?.day_loss;
  const cap = risk?.day_loss_cap ?? 0;
  const pctOfCap = cap > 0 && dl != null ? Math.min(100, Math.abs(dl) / cap * 100) : 0;
  return (
    <Card title="持仓风控" badge={<SrcPill ok="full" />}
          right={`positions_open=${risk?.positions_open ?? 0}`}>
      {pos ? (
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.8, mb: 1 }}>
          <Chip size="small"
                label={`${String(pos.direction).toUpperCase()} ${fmt(pos.lot, 2)} 手`}
                sx={{ height: 20, fontSize: 11, fontWeight: 600,
                      color: String(pos.direction).toUpperCase() === 'BUY' ? UD.long : UD.short,
                      backgroundColor: `${String(pos.direction).toUpperCase() === 'BUY' ? UD.long : UD.short}22` }} />
          {risk!.positions_open > 1 && (
            <Box sx={{ color: C.weak, fontSize: 10.5 }}>共 {risk!.positions_open} 笔</Box>
          )}
        </Box>
      ) : (
        <Box sx={{ color: C.weak, fontSize: 12, mb: 1 }}>当前无持仓</Box>
      )}
      <KV k="浮动盈亏合计"
          v={<Box component="span" sx={{ color: fp >= 0 ? UD.up : UD.down }}>{fmtSigned(fp)}</Box>} />
      <KV k="止损 SL" v={fmt(pos?.sl)} />
      <KV k="止盈 TP" v={fmt(pos?.tp)} />
      <KV k="开仓价 / 现价" v={`${fmt(pos?.open_price)} / ${fmt(pos?.current_price)}`} />
      <KV k="当日亏损（全账号，风控同口径）"
          v={<><Box component="span" sx={{ color: C.block }}>{fmtSigned(dl)}</Box>
              <Box component="span" sx={{ color: C.weak }}> / 阈值 {fmt(cap, 0)}</Box></>} />
      <Box sx={{ mt: 0.6, mb: 0.4, height: 4, borderRadius: 2, backgroundColor: C.inner, overflow: 'hidden' }}>
        <Box sx={{ width: `${pctOfCap}%`, height: '100%',
                   backgroundColor: pctOfCap > 70 ? C.block : pctOfCap > 40 ? C.warn : C.info }} />
      </Box>
      <KV k="trail_mult（directive）" v={dir.trail_mult ?? '—'} />
      <KV k="exit_ready / trail_lookback"
          v={<>{dir.exit_ready === true
            ? <Box component="span" sx={{ color: C.warn }}>true</Box>
            : String(dir.exit_ready ?? '—')} / {dir.trail_lookback ?? '—'}</>} />
      <KV k="sub_mode / is_trend" v={`${dir.sub_mode || '—'} / ${String(dir.is_trend ?? '—')}`} />
    </Card>
  );
};

/** 4 张卡片一行 */
export const StateCards = ({ live, risk }: { live: LiveResp | null; risk: RiskResp | null }) => (
  <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: '1fr 1fr', lg: 'repeat(4, 1fr)' },
             gap: 1.5, mb: 1.5 }}>
    <FsmCard live={live} />
    <TrendCard live={live} />
    <BoxCard live={live} />
    <RiskCard live={live} risk={risk} />
  </Box>
);

/* ── 三栏详情 ───────────────────────────────────────────────────────────── */
export const DetailPanels = ({ live, probeBar }: { live: LiveResp | null; probeBar: any }) => {
  const lv = live?.live || {};
  const ctx = live?.ctx || {};
  const cfg = live?.config || {};
  const dv = live?.derived;
  const fsm = live?.fsm || {};

  // 概率：优先用回放游标所在 bar（有历史），否则用实时快照
  const proba: Record<string, number> | null = lv.proba
    || (probeBar
      ? {
          oscillation: probeBar.prob_oscillation,
          trend_init: probeBar.prob_trend_init,
          trend_mid: probeBar.prob_trend_mid,
          trend_fade: probeBar.prob_trend_fade,
        } as Record<string, number>
      : null);

  const state = dv?.state ?? probeBar?.state ?? null;
  const isOsc = state === 'S1_OSC';
  const frozenByRule = !isOsc;

  return (
    <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', lg: 'repeat(3, 1fr)' },
               gap: 1.5, mb: 1.5 }}>

      {/* LGBM 模型面板 */}
      <Card title="LGBM 模型面板" badge={<SrcPill ok="partial" />}
            right={probeBar ? '回放 bar 值' : '实时快照'}>
        <Box sx={{ color: C.weak, fontSize: 11, mb: 0.6 }}>4 类预测概率（proba）</Box>
        {CLASS_KEYS.map((k) => {
          const v = proba ? Number(proba[k]) : NaN;
          const w = Number.isFinite(v) ? Math.max(0, Math.min(1, v)) * 100 : 0;
          return (
            <Box key={k} sx={{ display: 'flex', alignItems: 'center', gap: 1, my: 0.65 }}>
              <Box sx={{ width: 62, color: C.sub, fontSize: 11.5 }}>{CLASS_CN[k]}</Box>
              <Box sx={{ flex: 1, height: 9, borderRadius: 5, backgroundColor: C.inner, overflow: 'hidden' }}>
                <Box sx={{ width: `${w}%`, height: '100%', backgroundColor: CLASS_LINE_COLOR[k] }} />
              </Box>
              <Box sx={{ width: 52, textAlign: 'right', fontSize: 12, fontVariantNumeric: 'tabular-nums',
                         color: CLASS_LINE_COLOR[k] }}>{fmt(v, 3)}</Box>
            </Box>
          );
        })}
        <Box sx={{ mt: 1 }}>
          <KV k="原始分类 predicted_class"
              v={(probeBar?.predicted_class || lv.predicted_class)
                ? CLASS_CN[probeBar?.predicted_class || lv.predicted_class]
                : '—'} />
          <KV k="决策边际 margin"
              v={<Box component="span" sx={{ color: C.accent }}>{fmt(probeBar?.margin ?? lv.margin, 3)}</Box>} />
          <KV k="防抖计数 pending_streak" v={fsm.pending_streak ?? '—'} />
          <KV k="decided / infer_ok"
              v={`${String(lv.decided ?? '—')} / ${String(lv.infer_ok ?? '—')}`} />
          <KV k="age_bars / hold_only" v={`${fsm.age_bars ?? '—'} / ${String(fsm.hold_only ?? '—')}`} />
          <KV k="稳定行情态" v={<NoSrc reason="字段不存在（state_machine.py 无 stable 标记）" />} />
        </Box>
      </Card>

      {/* 箱体参数面板 —— 非 S1_OSC 整块置灰 */}
      <Card title="箱体参数面板" badge={<SrcPill ok="full" />}
            right={frozenByRule ? '非震荡态 → 冻结' : '震荡态'}>
        <Box sx={{ position: 'relative' }}>
          <Box sx={{ filter: frozenByRule ? 'grayscale(1)' : 'none', opacity: frozenByRule ? 0.42 : 1,
                     transition: 'opacity .2s' }}>
            <KV k="上沿 box_upper" v={fmt(ctx.box_upper)} />
            <KV k="下沿 box_lower" v={fmt(ctx.box_lower)} />
            <KV k="中轨 box_mid" v={fmt(ctx.box_mid)} />
            <KV k="箱体高度" v={<Box component="span" sx={{ color: BOX_COLOR }}>{fmt(dv?.box_height)}</Box>} />
            <KV k="state.box.window" v={cfg.box_window ? cfg.box_window.value : '—'} />
            <KV k="state.osc_box_min_width_atr" v={cfg.box_min_width_atr ? cfg.box_min_width_atr.value : '—'} />
            <KV k="box_frozen" v={String(ctx.box_frozen ?? '—')} />
            <KV k="box_frozen_at" v={ctx.box_frozen_at ? tsShort(ctx.box_frozen_at) : '—'} />
            <KV k="frozen_loss_count" v={ctx.frozen_loss_count ?? '—'} />
            <KV k="osc_round_active" v={String(ctx.osc_round_active ?? '—')} />
            <KV k="震荡段箱体状态" v={isOsc ? '有效' : '已离开震荡态'} />
          </Box>
          {frozenByRule && (
            <Box sx={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center',
                       justifyContent: 'center', backgroundColor: '#0d0d1499', borderRadius: 1 }}>
              <Box sx={{ color: '#cbd5e1', fontSize: 12, letterSpacing: 0.5, textAlign: 'center' }}>
                🔒 箱体冻结
                <Box sx={{ fontSize: 10.5, color: C.weak, mt: 0.4 }}>
                  当前 {state} ≠ S1_OSC
                </Box>
              </Box>
            </Box>
          )}
        </Box>
      </Card>

      {/* 趋势方向面板 */}
      <Card title="趋势方向面板" badge={<SrcPill ok="none" />} right="仅 direction 有源">
        <Box sx={{ fontWeight: 700, fontSize: 19, mb: 1, letterSpacing: 0.5,
                   color: (fsm.direction || 'none') === 'up' ? UD.up
                        : (fsm.direction || 'none') === 'down' ? UD.down : C.weak }}>
          {String(fsm.direction || 'none').toUpperCase()}
        </Box>
        <KV k="direction" v={fsm.direction || '—'} />
        <KV k="斜率 slope_atr" v={<NoSrc reason={dv?.trend_detail_reason} />} />
        <KV k="+DI" v={<NoSrc reason={dv?.trend_detail_reason} />} />
        <KV k="−DI" v={<NoSrc reason={dv?.trend_detail_reason} />} />
        <KV k="di_spread" v={<NoSrc reason={dv?.trend_detail_reason} />} />
        <KV k="方向防抖计数" v={<NoSrc reason="trend_direction.py:226-238 为纯窗口函数" />} />
        <Box sx={{ color: C.weak, fontSize: 10.5, mt: 1, lineHeight: 1.5 }}>
          +DI/−DI 目前仅作模型特征（state_features.py:53-54），落在 infer.feats，未进流/表/Redis。
        </Box>
      </Card>
    </Box>
  );
};
