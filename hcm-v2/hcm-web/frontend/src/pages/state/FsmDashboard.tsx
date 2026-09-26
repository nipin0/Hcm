import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert, Box, Chip, Popover, Paper, Slider, Table, TableBody, TableCell, TableContainer,
  TableHead, TableRow, ToggleButton, ToggleButtonGroup, Tooltip, Typography,
} from '@mui/material';
import client from '../../api/client';
import { ENDPOINTS } from '../../api/endpoints';
import { useSymbol } from '../../contexts/SymbolContext';
import KlineChart from './dashboard/KlineChart';
import ProbaChart from './dashboard/ProbaChart';
import { StateCards, DetailPanels } from './dashboard/Panels';
import { C, STATE_COLOR, CLASS_CN, fmt, tsShort } from './dashboard/theme';
import type { KlineBar, LiveResp, RiskResp, LogRow, Envelope } from './dashboard/theme';

/**
 * FSM 行情状态机看板（只读）
 * =====================================================================
 * 数据来源：GET /api/v1/state/{live,logs,kline,risk}/{symbol}（见 hcm-web/web/api/state.py）
 *
 * 铁律合规：
 *  - 本页**纯展示**：不写任何配置、不触发任何决策、不干预下单链路。
 *  - 无数据源的字段一律**如实标注「无数据源」**，不伪造 0、不用 UI 规则掩盖字段真值。
 *    具体：斜率/+DI/−DI/方向防抖计数/稳定行情态 → 后端当前确实没有（见各面板说明）。
 *  - 「非 S1 置灰箱体」是**视觉规则**，与后端 box_frozen 字段分开呈现；两者不一致时
 *    如实并列展示（derived.freeze_rule_misaligned）。
 */

const INTERVALS = [3, 5, 10];

export default function FsmDashboard() {
  const { selectedSymbol } = useSymbol();
  // 与 HexpDashboard.tsx:40 同写法 —— selectedSymbol 是 SymbolInfo 对象，不是字符串
  const symbol = (selectedSymbol?.symbol ?? 'XAUUSD').toUpperCase();

  const [tf, setTf] = useState('M5');
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [intervalSec, setIntervalSec] = useState(5);
  /** 刷新触发方式：'bar' = K 线闭合才全量刷新（只轻量轮询 /risk 与最新 bar 时间戳）；'poll' = 每个间隔全量刷新 */
  const [refreshMode, setRefreshMode] = useState<'bar' | 'poll'>('bar');
  const [alertOn, setAlertOn] = useState(true);
  const [alertAnchorEl, setAlertAnchorEl] = useState<HTMLElement | null>(null);
  const [replayOn, setReplayOn] = useState(false);
  const [cursor, setCursor] = useState(0);

  const [live, setLive] = useState<LiveResp | null>(null);
  const [risk, setRisk] = useState<RiskResp | null>(null);
  const [bars, setBars] = useState<KlineBar[]>([]);
  const [logs, setLogs] = useState<LogRow[]>([]);
  const [err, setErr] = useState('');
  const [lastTs, setLastTs] = useState('');

  const timerRef = useRef<number | null>(null);
  const replayRef = useRef(false);
  /** 已见最新 K 线开盘时间 —— 用于判定"K 线闭合" */
  const lastBarRef = useRef('');
  useEffect(() => { replayRef.current = replayOn; }, [replayOn]);

  /* ── 数据加载 ─────────────────────────────────────────────────────────── */
  const loadAll = useCallback(async () => {
    try {
      const [rLive, rRisk, rKline, rLogs] = await Promise.all([
        client.get(ENDPOINTS.state.live(symbol)),
        client.get(ENDPOINTS.state.risk(symbol)),
        client.get(ENDPOINTS.state.kline(symbol), { params: { tf, limit: 300 } }),
        client.get(ENDPOINTS.state.logs(symbol), { params: { tf, limit: 200 } }),
      ]);
      const msg: string[] = [];
      const ok = <T,>(r: any, name: string): T | null => {
        const env = (r?.data ?? {}) as Envelope<T>;
        if (env.code !== 0 || env.data == null) { msg.push(`${name}: ${env.message || env.code}`); return null; }
        return env.data;
      };
      const dLive = ok<LiveResp>(rLive, 'live');
      const dRisk = ok<RiskResp>(rRisk, 'risk');
      const dKline = ok<{ items: KlineBar[] }>(rKline, 'kline');
      const dLogs = ok<{ items: LogRow[] }>(rLogs, 'logs');

      setLive(dLive);
      setRisk(dRisk);
      const newBars = dKline?.items || [];
      // 休市/无数据时 newBars 也可能为空 —— 不清空旧 bars，避免图表闪空
      if (newBars.length) {
        setBars(newBars);
        // /kline 是 ASC 返回，末根即最新 → 记录已见最新 bar，供"K 线闭合"判定
        lastBarRef.current = String(newBars[newBars.length - 1].open_time);
        setCursor((prev) => (replayRef.current ? Math.min(prev, newBars.length - 1) : newBars.length - 1));
      }
      if (dLogs?.items) setLogs(dLogs.items);
      setErr(msg.join(' · '));
      setLastTs(new Date().toLocaleTimeString('zh-CN', { hour12: false }));
    } catch (e: any) {
      setErr(String(e?.message || e));
    }
  }, [symbol, tf]);

  /** 轻量刷新：只取持仓/浮亏/当日亏损（K 线闭合模式下每 tick 都跑，保证风控卡不滞后） */
  const loadRiskOnly = useCallback(async () => {
    try {
      const r = await client.get(ENDPOINTS.state.risk(symbol));
      const env = (r?.data ?? {}) as Envelope<RiskResp>;
      if (env.code === 0 && env.data) setRisk(env.data);
    } catch {
      /* 轻量刷新失败不打断主循环，也不覆盖已有数据 */
    }
  }, [symbol]);

  /**
   * K 线闭合探测（需求「K线闭合触发页面自动刷新」的实现）。
   * 只取最新 1 根 bar 的 open_time（/proba 是窄响应），与已见值比对：
   * 变化 ⇒ 新 K 线已闭合 ⇒ 返回 true，由调用方触发一次全量刷新。
   * 注意 /proba 内部按 open_time DESC 取 limit 根后 reverse，故 limit=1 时 items[0] 即最新。
   */
  const detectBarClose = useCallback(async (): Promise<boolean> => {
    try {
      const r = await client.get(ENDPOINTS.state.proba(symbol), { params: { tf, limit: 1 } });
      const env = (r?.data ?? {}) as Envelope<{ items: LogRow[] }>;
      const t = env.code === 0 ? env.data?.items?.[0]?.bar_open_time : null;
      if (!t) return false;
      const seen = lastBarRef.current;
      lastBarRef.current = String(t);
      // 首次（seen 为空）不算闭合，避免挂载瞬间多刷一次
      return !!seen && String(t) !== seen;
    } catch {
      return false;
    }
  }, [symbol, tf]);

  /* 轮询 + 页面不可见时暂停
     refreshMode='bar' ：每 tick 只做「轻量刷新(/risk) + K 线闭合探测」，闭合时才全量刷新
     refreshMode='poll'：每 tick 全量刷新（无差别轮询） */
  useEffect(() => {
    loadAll();
    const tick = async () => {
      if (document.visibilityState !== 'visible') return;
      if (refreshMode === 'poll') { await loadAll(); return; }
      await loadRiskOnly();
      // 回放中不因新 K 线闭合而跳回最新，避免打断历史复现
      if (!replayRef.current && await detectBarClose()) await loadAll();
    };
    const start = () => {
      if (timerRef.current !== null) return;
      timerRef.current = window.setInterval(tick, intervalSec * 1000);
    };
    const stop = () => {
      if (timerRef.current !== null) { window.clearInterval(timerRef.current); timerRef.current = null; }
    };
    const onVis = () => {
      if (document.visibilityState === 'visible') { tick(); start(); } else stop();
    };
    if (autoRefresh) start(); else stop();
    document.addEventListener('visibilitychange', onVis);
    return () => { stop(); document.removeEventListener('visibilitychange', onVis); };
  }, [loadAll, loadRiskOnly, detectBarClose, autoRefresh, intervalSec, refreshMode]);

  /* ── 告警：概率大幅跳变 / 状态频繁抖动（纯前端计算）──────────────────── */
  const alerts = useMemo(() => {
    const out: { sev: 'red' | 'amber'; txt: string }[] = [];
    for (let i = 1; i < bars.length; i++) {
      const keys = ['prob_oscillation', 'prob_trend_init', 'prob_trend_mid', 'prob_trend_fade'] as const;
      let d = 0;
      keys.forEach((k) => {
        const a = Number((bars[i] as any)[k]);
        const b = Number((bars[i - 1] as any)[k]);
        if (Number.isFinite(a) && Number.isFinite(b)) d = Math.max(d, Math.abs(a - b));
      });
      if (d > 0.24) out.push({ sev: 'red', txt: `概率跳变 Δ${d.toFixed(3)} @${tsShort(bars[i].open_time)}` });
    }
    for (let i = 1; i < bars.length; i++) {
      if (bars[i].transitioned && Number(bars[i].age_bars) <= 2) {
        for (let j = i + 1; j < Math.min(i + 13, bars.length); j++) {
          if (bars[j].transitioned) {
            out.push({
              sev: 'amber',
              txt: `状态抖动 ${bars[i].state}→${bars[j].state} 间隔 ${j - i} 根 @${tsShort(bars[j].open_time)}`,
            });
            break;
          }
        }
      }
    }
    return out.slice(-6).reverse();
  }, [bars]);

  /* ── 回放视图 ─────────────────────────────────────────────────────────── */
  const viewBars = useMemo(() => bars.slice(0, Math.max(1, cursor + 1)), [bars, cursor]);
  const probeBar = viewBars[viewBars.length - 1] || null;
  const curTime = probeBar?.open_time || '';

  const viewLogs = useMemo(() => {
    if (!curTime) return logs.slice(0, 40);
    return logs.filter((r) => String(r.bar_open_time) <= curTime).slice(0, 40);
  }, [logs, curTime]);

  const jumpToOsc = () => {
    for (let i = bars.length - 1; i >= 0; i--) {
      if (bars[i].state === 'S1_OSC') { setReplayOn(true); setCursor(i); return; }
    }
  };

  const cardHead = (title: string, sub: string, right?: React.ReactNode) => (
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1 }}>
      <Typography sx={{ color: C.text, fontSize: 13, fontWeight: 600 }}>{title}</Typography>
      <Typography sx={{ color: C.weak, fontSize: 11 }}>{sub}</Typography>
      <Box sx={{ ml: 'auto' }}>{right}</Box>
    </Box>
  );

  return (
    <Box sx={{ p: 2, minHeight: '100vh', backgroundColor: C.bg }}>

      {/* ── 顶部导航条 ── */}
      <Paper elevation={0} sx={{ backgroundColor: C.card, border: `1px solid ${C.border}`,
                                 borderRadius: 2, p: 1.5, mb: 1.5,
                                 display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 2 }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.8 }}>
          <Typography sx={{ color: C.weak, fontSize: 11 }}>品种</Typography>
          <Chip size="small" label={symbol} sx={{ height: 22, fontSize: 11.5, color: C.text,
                 backgroundColor: C.inner, border: `1px solid ${C.border}` }} />
          <Tooltip title="品种由左侧全局选择器驱动（src/components/SymbolSelector.tsx），此处不重复放置以免与全局脱钩">
            <Typography sx={{ color: C.weak, fontSize: 10 }}>全局</Typography>
          </Tooltip>
        </Box>

        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.8 }}>
          <Typography sx={{ color: C.weak, fontSize: 11 }}>周期</Typography>
          <ToggleButtonGroup exclusive size="small" value={tf} onChange={(_, v) => v && setTf(v)}>
            <ToggleButton value="M5" sx={{ fontSize: 11, px: 1.4, py: 0.2, color: C.sub }}>M5</ToggleButton>
            <Tooltip title="磁盘无 M15 模型文件，且 hcm_market.klines_xauusd 无 M15 数据（P2 待补）">
              <span><ToggleButton disabled value="M15" sx={{ fontSize: 11, px: 1.4, py: 0.2 }}>M15</ToggleButton></span>
            </Tooltip>
            <Tooltip title="磁盘无 H1 模型文件（仅 lgbm_state_M5_v1/v2/v3）">
              <span><ToggleButton disabled value="H1" sx={{ fontSize: 11, px: 1.4, py: 0.2 }}>H1</ToggleButton></span>
            </Tooltip>
          </ToggleButtonGroup>
        </Box>

        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.8 }}>
          <Typography sx={{ color: C.weak, fontSize: 11 }}>刷新</Typography>
          <ToggleButtonGroup exclusive size="small" value={intervalSec}
                             onChange={(_, v) => v && setIntervalSec(v)}>
            {INTERVALS.map((s) => (
              <ToggleButton key={s} value={s} sx={{ fontSize: 11, px: 1.2, py: 0.2, color: C.sub }}>
                {s}s
              </ToggleButton>
            ))}
          </ToggleButtonGroup>
          <Chip size="small" label={autoRefresh ? '自动刷新' : '已暂停'}
                onClick={() => setAutoRefresh((v) => !v)}
                sx={{ height: 22, fontSize: 11, cursor: 'pointer',
                      color: autoRefresh ? C.ok : C.weak,
                      backgroundColor: autoRefresh ? '#0e2418' : C.inner,
                      border: `1px solid ${autoRefresh ? '#2a5f3f' : C.border}` }} />
          {/* 触发方式：K 线闭合（默认）vs 无差别轮询 */}
          <ToggleButtonGroup exclusive size="small" value={refreshMode}
                             onChange={(_, v) => v && setRefreshMode(v)}>
            <Tooltip title="每 5s 轻量探测最新 K 线开盘时间，仅在 K 线闭合时全量刷新（持仓卡仍每 tick 更新）">
              <ToggleButton value="bar" sx={{ fontSize: 10.5, px: 1, py: 0.2, color: C.sub }}>
                K线闭合
              </ToggleButton>
            </Tooltip>
            <Tooltip title="每个间隔都全量刷新（4 个端点）">
              <ToggleButton value="poll" sx={{ fontSize: 10.5, px: 1, py: 0.2, color: C.sub }}>
                轮询
              </ToggleButton>
            </Tooltip>
          </ToggleButtonGroup>
        </Box>

        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.8, minWidth: 320, flex: 1 }}>
          <Typography sx={{ color: C.weak, fontSize: 11 }}>回放</Typography>
          <Chip size="small" label={replayOn ? '历史回放' : '跟随最新'}
                onClick={() => { const v = !replayOn; setReplayOn(v);
                                 setCursor(v ? Math.floor(bars.length * 0.6) : bars.length - 1); }}
                sx={{ height: 22, fontSize: 11, cursor: 'pointer',
                      color: replayOn ? C.accent : C.weak,
                      backgroundColor: replayOn ? '#2a1a3a' : C.inner,
                      border: `1px solid ${replayOn ? '#5b3a80' : C.border}` }} />
          <Slider size="small" min={0} max={Math.max(0, bars.length - 1)} value={Math.min(cursor, Math.max(0, bars.length - 1))}
                  onChange={(_, v) => { setReplayOn(true); setCursor(v as number); }}
                  sx={{ color: C.info, py: 0.5 }} />
          <Typography sx={{ color: C.sub, fontSize: 11, minWidth: 92 }}>
            {probeBar ? tsShort(probeBar.open_time) : '—'}
          </Typography>
          <Chip size="small" label="跳到 S1 段" onClick={jumpToOsc}
                sx={{ height: 20, fontSize: 10.5, cursor: 'pointer', color: C.sub, backgroundColor: C.inner }} />
          <Chip size="small" label="回到最新" onClick={() => { setReplayOn(false); setCursor(bars.length - 1); }}
                sx={{ height: 20, fontSize: 10.5, cursor: 'pointer', color: C.sub, backgroundColor: C.inner }} />
        </Box>

        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, ml: 'auto' }}>
          <Chip size="small" label={alertOn ? '告警开' : '告警关'} onClick={() => setAlertOn((v) => !v)}
                sx={{ height: 22, fontSize: 11, cursor: 'pointer',
                      color: alertOn ? C.warn : C.weak,
                      backgroundColor: alertOn ? '#231c08' : C.inner,
                      border: `1px solid ${alertOn ? '#6b5416' : C.border}` }} />
          {/* 右上角告警指示器：常态只显示计数，点击弹出明细 */}
          <Chip size="small"
                label={alertOn ? `告警 ${alerts.length}` : '告警已关'}
                onClick={(e) => setAlertAnchorEl(e.currentTarget)}
                sx={{ height: 22, fontSize: 11, cursor: 'pointer',
                      color: !alertOn ? C.weak
                           : alerts.some((a) => a.sev === 'red') ? C.block
                           : alerts.length ? C.warn : C.sub,
                      backgroundColor: !alertOn ? C.inner
                           : alerts.some((a) => a.sev === 'red') ? '#240f0f'
                           : alerts.length ? '#231c08' : C.inner,
                      border: `1px solid ${!alertOn ? C.border
                           : alerts.some((a) => a.sev === 'red') ? '#6b2424'
                           : alerts.length ? '#6b5416' : C.border}` }} />
          <Popover open={!!alertAnchorEl} anchorEl={alertAnchorEl}
                   onClose={() => setAlertAnchorEl(null)}
                   anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
                   transformOrigin={{ vertical: 'top', horizontal: 'right' }}>
            <Box sx={{ backgroundColor: C.card, border: `1px solid ${C.border}`, p: 1.4,
                       maxWidth: 460, maxHeight: 320, overflow: 'auto' }}>
              <Typography sx={{ color: C.text, fontSize: 12, fontWeight: 600, mb: 0.8 }}>
                异常告警（{alerts.length}）
              </Typography>
              {!alertOn && (
                <Typography sx={{ color: C.weak, fontSize: 11 }}>告警已关闭</Typography>
              )}
              {alertOn && alerts.length === 0 && (
                <Typography sx={{ color: C.weak, fontSize: 11 }}>当前无异常</Typography>
              )}
              {alertOn && alerts.map((a, i) => (
                <Box key={i} sx={{ display: 'flex', gap: 0.8, alignItems: 'baseline', py: 0.4,
                                   borderBottom: `1px dashed ${C.divider}` }}>
                  <Box sx={{ width: 6, height: 6, borderRadius: '50%', mt: 0.6,
                             backgroundColor: a.sev === 'red' ? C.block : C.warn }} />
                  <Typography sx={{ color: a.sev === 'red' ? C.block : C.warn, fontSize: 11 }}>
                    {a.txt}
                  </Typography>
                </Box>
              ))}
              <Typography sx={{ color: C.weak, fontSize: 10, mt: 1, lineHeight: 1.5 }}>
                判定口径（纯前端，基于已加载 K 线）：相邻 bar 的 4 类概率最大变化 &gt; 0.24 记为「概率跳变」；
                两次 transitioned 间隔 ≤ 12 根记为「状态抖动」。
              </Typography>
            </Box>
          </Popover>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.6 }}>
            <Box sx={{ width: 8, height: 8, borderRadius: '50%',
                       backgroundColor: autoRefresh ? C.ok : C.weak }} />
            <Typography sx={{ color: C.sub, fontSize: 11 }}>
              {replayOn ? 'REPLAY' : autoRefresh ? 'LIVE' : 'PAUSED'} · {tf} ·{' '}
              {refreshMode === 'bar' ? 'K线闭合' : '轮询'}
              {lastTs ? ` · ${lastTs}` : ''}
            </Typography>
          </Box>
        </Box>
      </Paper>

      {err && (
        <Alert severity="warning" sx={{ mb: 1.5, backgroundColor: '#231c08', color: C.text,
                                        border: '1px solid #6b5416', fontSize: 12 }}>
          部分数据源未就绪：{err}
        </Alert>
      )}

      {/* 告警明细已上移至右上角指示器（点击 Popover 展开），此处不再占一整行 */}

      {/* ── 4 张状态卡片 ── */}
      <StateCards live={live} risk={risk} />

      {/* ── 箱体双图（左右并排）【2026-09-25 需求：把 K 线主图拆成 55/61 两张箱体图】── */}
      <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', lg: '1fr 1fr' },
                 gap: 1.5, mb: 1.5 }}>
        <Paper elevation={0} sx={{ backgroundColor: C.card, border: `1px solid ${C.border}`,
                                   borderRadius: 2, p: 2 }}>
          {cardHead('箱体图 ① · Magic 61（FSM S1 箱）',
                    'hcm_market.klines_xauusd ⋈ hcm_signal.market_state_log（open_time = bar_open_time）· '
                    + '三线 = 塔逐 bar 落库的 intent.box_*（冻结感知）· 开平仓取自 orders',
                    <Typography sx={{ color: C.weak, fontSize: 10.5 }}>
                      ⚠ 标记点无 position_id 关联，按时间就近吸附（近似）
                    </Typography>)}
          <KlineChart bars={bars} cursor={cursor} live={live} boxKind="m61"
                      orders={risk?.recent_orders || []}
                      positions={risk?.positions || []} />
        </Paper>

        <Paper elevation={0} sx={{ backgroundColor: C.card, border: `1px solid ${C.border}`,
                                   borderRadius: 2, p: 2 }}>
          {cardHead('箱体图 ② · Magic 55（RANGE 均值回归 · 快箱）',
                    'hcm_market.klines_xauusd ⋈ hcm_signal.range_box_log（open_time = bar_open_time）· '
                    + '三线 = 塔逐 bar 落库的 range_box **fast 箱**（与 Redis 快照同一次计算）',
                    <Typography sx={{ color: C.weak, fontSize: 10.5 }}>
                      ⚠ 该表自 2026-09-25 起逐 bar 落库（历史不可回填）· 55 每 bar 重算、无冻结/轮次
                    </Typography>)}
          <KlineChart bars={bars} cursor={cursor} live={live} boxKind="m55"
                      orders={risk?.recent_orders || []}
                      positions={risk?.positions || []} />
        </Paper>
      </Box>

      <Paper elevation={0} sx={{ backgroundColor: C.card, border: `1px solid ${C.border}`,
                                 borderRadius: 2, p: 2, mb: 1.5 }}>
        {cardHead('LGBM 概率时序 · 4 类预测 + 状态切换节点',
                  'prob_oscillation / prob_trend_init / prob_trend_mid / prob_trend_fade · 虚线 = transitioned',
                  <Typography sx={{ color: C.weak, fontSize: 10.5 }}>
                    共 {bars.length} 根，切换 {bars.filter((b) => b.transitioned).length} 次
                  </Typography>)}
        <ProbaChart bars={bars} cursor={cursor} />
      </Paper>

      {/* ── 三栏详情 ── */}
      <DetailPanels live={live} probeBar={probeBar} />

      {/* ── 底部日志 ── */}
      <Paper elevation={0} sx={{ backgroundColor: C.card, border: `1px solid ${C.border}`,
                                 borderRadius: 2, p: 2 }}>
        {cardHead('状态机日志', 'hcm_signal.market_state_log',
                  <Typography sx={{ color: C.block, fontSize: 10.5 }}>
                    ⚠ 信号类型 / 风控结果：表中无 signal_mode 列、无 signal_id，当前无数据源
                  </Typography>)}
        <TableContainer sx={{ maxHeight: 300 }}>
          <Table size="small" stickyHeader>
            <TableHead>
              <TableRow>
                {['时间戳', 'K线序号', 'FSM状态', '行情态', '趋势方向', '事件',
                  '信号类型', '触发原因', '风控结果', 'age_bars', 'margin'].map((h) => (
                  <TableCell key={h} sx={{ color: C.sub, fontSize: 11,
                                           backgroundColor: C.inner, whiteSpace: 'nowrap' }}>{h}</TableCell>
                ))}
              </TableRow>
            </TableHead>
            <TableBody>
              {viewLogs.map((r, i) => {
                const sc = STATE_COLOR[r.state] || C.idle;
                return (
                  <TableRow key={`${r.id ?? i}`} hover>
                    <TableCell sx={{ color: C.weak, fontSize: 11 }}>{tsShort(r.bar_open_time)}</TableCell>
                    <TableCell sx={{ color: C.weak, fontSize: 11 }}>
                      #{r.id ?? '—'}<Box component="span" sx={{ color: C.weak }}> / age {r.age_bars ?? '—'}</Box>
                    </TableCell>
                    <TableCell sx={{ fontSize: 11 }}>
                      <Box component="span" sx={{ color: sc }}>● {r.state || '—'}</Box>
                    </TableCell>
                    <TableCell sx={{ color: C.text, fontSize: 11 }}>
                      {r.predicted_class ? (CLASS_CN[r.predicted_class] || r.predicted_class) : '—'}
                    </TableCell>
                    <TableCell sx={{ color: C.weak, fontSize: 11 }}>{r.direction || '—'}</TableCell>
                    <TableCell sx={{ fontSize: 11 }}>
                      {r.transitioned
                        ? <Box component="span" sx={{ color: C.info }}>state_transition</Box>
                        : (r.note || 'same_state')}
                    </TableCell>
                    <TableCell sx={{ color: C.block, fontSize: 10.5, fontStyle: 'italic' }}>无数据源</TableCell>
                    <TableCell sx={{ color: C.weak, fontSize: 11 }}>{r.trigger_reason || '—'}</TableCell>
                    <TableCell sx={{ color: C.block, fontSize: 10.5, fontStyle: 'italic' }}>无数据源</TableCell>
                    <TableCell sx={{ color: C.weak, fontSize: 11 }}>{r.age_bars ?? '—'}</TableCell>
                    <TableCell sx={{ color: C.accent, fontSize: 11 }}>{fmt(r.margin, 3)}</TableCell>
                  </TableRow>
                );
              })}
              {!viewLogs.length && (
                <TableRow>
                  <TableCell colSpan={11} sx={{ color: C.weak, fontSize: 11, textAlign: 'center' }}>
                    暂无数据（休市或状态日志尚未写入）
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </TableContainer>
      </Paper>
    </Box>
  );
}
