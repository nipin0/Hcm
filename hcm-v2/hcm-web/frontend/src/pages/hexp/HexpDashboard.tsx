/** 和乘幂信号状态看板 · /hexp/dashboard
 *
 * 纯观测页面：只读 `GET /api/v1/hexp/signal/{symbol}` 与 `GET /api/v1/hexp/config`，
 * **不发起任何写操作**，不影响引擎决策与下单。
 *
 * 数据链路：
 *   hexp_engine（约 3s 重算）→ Redis `hcm:live:hexp:{symbol}`（TTL 15s）
 *   → hcm-web `/api/v1/hexp/signal/{symbol}` → 本页轮询
 *
 * 信号日志采用**前端环形缓冲**（v1 零后端改动），刷新页面即清空。
 */
import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import {
  Box, Typography, Tooltip, ToggleButton, ToggleButtonGroup, CircularProgress,
} from '@mui/material';
import client from '../../api/client';
import { ENDPOINTS } from '../../api/endpoints';
import { useSymbol } from '../../contexts/SymbolContext';
import DecisionPanel from './dashboard/DecisionPanel';
import HpCorePanel from './dashboard/HpCorePanel';
import ResonancePanel from './dashboard/ResonancePanel';
import ScorecardPanel from './dashboard/ScorecardPanel';
import MicroPanel from './dashboard/MicroPanel';
import ExecutionPanel from './dashboard/ExecutionPanel';
import LogPanel from './dashboard/LogPanel';
import {
  C, snapshotAgeSec, inferEntryMode, diffForLog, LOG_CAPACITY,
  KLINE_INTERVAL_SEC, klineFreshness,
} from './dashboard/types';
import type { HexpSnapshot, HexpConfig, LogEntry, KlineIngest, HexpAiSnapshot } from './dashboard/types';

/** HP-Score 变化超过该幅度才计入「仅记录变化」日志 */
const HP_LOG_DELTA = 1.0;

/** 可选轮询间隔（秒）。引擎约 3s 重算一次，默认 3s 对齐 */
const INTERVALS = [3, 5, 10] as const;

const HexpDashboard: React.FC = () => {
  const { selectedSymbol } = useSymbol();
  const symbol = selectedSymbol?.symbol ?? 'XAUUSD';

  const [snap, setSnap] = useState<HexpSnapshot | null>(null);
  const [cfg, setCfg] = useState<HexpConfig | null>(null);
  const [activeModel, setActiveModel] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [firstLoad, setFirstLoad] = useState<boolean>(true);
  const [intervalSec, setIntervalSec] = useState<number>(3);
  const [paused, setPaused] = useState<boolean>(false);

  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [onlyChanges, setOnlyChanges] = useState<boolean>(true);

  /** K 线入库实时状态（按周期）：{ M5: KlineIngest, H1: ... } */
  const [klineStatus, setKlineStatus] = useState<Record<string, KlineIngest>>({});
  /** 行情源（bridge 2s tick）最新更新距今秒数；null=未知 */
  const [feedAgeSec, setFeedAgeSec] = useState<number | null>(null);
  /** AI 信号质量评分快照（AI评分/总分/外部因子评分三卡）；null=未启用 */
  const [aiSnap, setAiSnap] = useState<HexpAiSnapshot | null>(null);

  /** 用于计算快照新鲜度的秒级心跳 */
  const [, setTick] = useState<number>(0);
  const logIdRef = useRef<number>(0);
  const lastTsRef = useRef<number>(0);
  const onlyChangesRef = useRef<boolean>(true);
  const cfgRef = useRef<HexpConfig | null>(null);

  useEffect(() => { onlyChangesRef.current = onlyChanges; }, [onlyChanges]);
  useEffect(() => { cfgRef.current = cfg; }, [cfg]);

  // ── 配置（阈值/权重/执行参数）──
  const loadConfig = useCallback(async (): Promise<void> => {
    try {
      const res = await client.get(ENDPOINTS.hexp.config);
      if (res.data?.code === 0 && res.data?.data) setCfg(res.data.data as HexpConfig);
    } catch {
      /* 配置读取失败时组件回落到与后端一致的默认值，不阻塞看板 */
    }
  }, []);

  // ── 当前激活模型（用于「未激活」提示）──
  // 真实路由键是配置中心 `signal.active_model`（scheduler._detect_active_model 用它做机制路由），
  // 与 `signal_tower.mode` 是两个不同的概念：
  //   · signal_tower.mode = ai_dynamic / manual / co_source（仅区分手动镜像 vs 自动模型路由）
  //   · signal.active_model = default / hexp（真正决定走哪条评分链路；
  //     【2026-08-28】双源 co_source 已下线，不再是可选取值）
  // 因此必须读 engine-mode 配置端点（原 cosource 端点），取 `signal.active_model`；
  // 读 `signal_tower.mode` 会误判。
  // 参考实现：src/pages/signaltower/Mode.tsx fetchActiveMode()
  const loadActiveModel = useCallback(async (): Promise<void> => {
    try {
      const res = await client.get(ENDPOINTS.engineMode.config);
      const d = res.data?.data ?? {};
      const v = d['signal.active_model'] ?? null;
      if (v === null || v === undefined) return;
      setActiveModel(typeof v === 'string' ? v : String(v));
    } catch {
      /* 提示性信息，失败不影响主功能 */
    }
  }, []);

  /** 追加一条环形缓冲日志（容量上限 LOG_CAPACITY，最新在前） */
  const pushLog = useCallback((s: HexpSnapshot): void => {
    setLogs((prev) => {
      const mode = inferEntryMode(s, cfgRef.current).mode;
      const change = diffForLog(prev[0], s, mode, HP_LOG_DELTA);
      if (onlyChangesRef.current && change === null) return prev;
      logIdRef.current += 1;
      const entry: LogEntry = {
        id: logIdRef.current,
        ts: s.ts,
        direction: s.direction,
        grade: s.grade,
        hpScore: s.hp_score,
        k: s.k,
        total: s.scorecard_total,
        passed: s.passed,
        verdict: s.verdict,
        primaryState: (s.period_states ?? {})[s.primary_period] ?? '',
        entryMode: mode,
        reason: s.reason,
        change: change ?? '心跳',
      };
      return [entry, ...prev].slice(0, LOG_CAPACITY);
    });
  }, []);

  // ── 信号快照轮询 ──
  const loadSignal = useCallback(async (): Promise<void> => {
    try {
      const res = await client.get(`${ENDPOINTS.hexp.signal}/${symbol}`);
      const body = res.data;
      if (body?.code === 0) {
        const d = body.data as HexpSnapshot | null;
        if (d) {
          setSnap(d);
          setError(null);
          // 仅在快照真正更新（ts 变化）时记录日志，避免重复帧刷屏
          if (d.ts !== lastTsRef.current) {
            lastTsRef.current = d.ts;
            pushLog(d);
          }
        } else {
          setError(body.message === 'no_signal_yet'
            ? '引擎尚无该品种的实时快照（和乘幂可能未激活，或行情源中断）'
            : (body.message ?? '无数据'));
        }
      } else {
        setError(body?.message ?? '接口返回异常');
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '请求失败');
    } finally {
      setFirstLoad(false);
    }
  }, [symbol, pushLog]);

  // ── K 线入库实时状态（慢轮询，独立于 3s 信号轮询）──
  // 主数据源：新加的只读端点 `GET /api/v1/dashboard/klines/latest/{symbol}`，直接读
  //   Redis `latest_kline:{symbol}:{tf}`（collector 每 tick 写入，最实时），单次返回全周期。
  // 兜底：若该端点尚未部署（hcm-web 未重启），回退到现有 `GET /api/v1/dashboard/realtime`
  //   按周期取 PG `hcm_market.klines` 的最新棒（归档视角，可能滞后）。
  // 两者均零写操作，不影响入库/引擎。
  // 【2026-08-24】加入 M30，与引擎 hexp.periods(含 M30) 的 K 线入库状态保持一致；
  // 此前 M30 不在列表 → klineStatus 无 M30 → 共振矩阵 M30 卡片无 K 线行。
  const KLINE_PERIODS = ['M5', 'M30', 'H1', 'H4', 'D1'];

  /** 把任意 open_time（ISO 串或 unix 秒/毫秒）解析为 unix 秒 */
  const parseOpenTime = (v: unknown): number | null => {
    if (v === null || v === undefined) return null;
    if (typeof v === 'number' && Number.isFinite(v)) {
      return v > 1e12 ? v / 1000 : v; // 毫秒→秒
    }
    if (typeof v === 'string') {
      const t = new Date(v.replace('Z', '+00:00')).getTime();
      if (Number.isFinite(t)) return t / 1000;
    }
    return null;
  };

  const loadKlineStatus = useCallback(async (sym: string): Promise<void> => {
    // 1) 主路径：Redis 实时端点
    try {
      const res = await client.get(`/api/v1/dashboard/klines/latest/${encodeURIComponent(sym)}`);
      const data = res.data?.data;
      if (data && typeof data === 'object') {
        const out: Record<string, KlineIngest> = {};
        let feedAge: number | null = null;
        for (const p of KLINE_PERIODS) {
          const kl = data[p];
          const openTime = kl ? parseOpenTime(kl.open_time) : null;
          const intervalSec = KLINE_INTERVAL_SEC[p] ?? 300;
          const ageSec = openTime ? Math.max(0, Date.now() / 1000 - openTime) : null;
          out[p] = {
            openTime,
            close: kl?.close ?? null,
            ageSec,
            fresh: klineFreshness(openTime, intervalSec),
            tickCount: kl?.tick_count ?? kl?.tick_volume ?? null,
          };
        }
        // 行情源实时性必须以 bridge 每 2s 写入的真实 tick 时间戳为准
        // （data.live_price.updated_at），绝不能用 M5 棒的「距棒起始」秒数——
        // 一根 300s 的 M5 棒进行到中段就 ~150s，会被 feedStatusMeta 误判「断开」。
        // 真实 tick 缺失（端点尚未返回 live_price）时回退 null → 显示「未知」，比误报断开温和。
        const lp = data.live_price;
        if (lp && lp.updated_at) {
          feedAge = Math.max(0, Date.now() / 1000 - Number(lp.updated_at));
        }
        setKlineStatus(out);
        setFeedAgeSec(feedAge);
        return;
      }
    } catch {
      /* 端点未部署（hcm-web 未重启）→ 走兜底 */
    }
    // 2) 兜底：逐周期 PG realtime 端点
    void Promise.all(
      KLINE_PERIODS.map(async (p): Promise<[string, KlineIngest]> => {
        try {
          const res = await client.get(
            `/api/v1/dashboard/realtime?symbol=${encodeURIComponent(sym)}&timeframe=${p}`,
          );
          const d = res.data?.data;
          const kl = d?.latest_kline;
          const openTime = kl ? parseOpenTime(kl.open_time) : null;
          const intervalSec = KLINE_INTERVAL_SEC[p] ?? 300;
          const ageSec = openTime ? Math.max(0, Date.now() / 1000 - openTime) : null;
          const fresh = klineFreshness(openTime, intervalSec);
          if (p === 'M5' && d?.live_price?.updated_at) {
            setFeedAgeSec(Math.max(0, Date.now() / 1000 - Number(d.live_price.updated_at)));
          }
          return [p, { openTime, close: kl?.close ?? null, ageSec, fresh, tickCount: null }];
        } catch {
          return [p, { openTime: null, close: null, ageSec: null, fresh: 'none' as const, tickCount: null }];
        }
      }),
    ).then((entries) => {
      setKlineStatus(Object.fromEntries(entries));
    });
  }, []);

  // 品种切换：重置快照与日志
  useEffect(() => {
    setSnap(null);
    setLogs([]);
    setAiSnap(null);
    lastTsRef.current = 0;
    setFirstLoad(true);
  }, [symbol]);

  useEffect(() => {
    void loadConfig();
    void loadActiveModel();
  }, [loadConfig, loadActiveModel]);

  useEffect(() => {
    if (paused) return undefined;
    void loadSignal();
    const t = setInterval(() => { void loadSignal(); }, intervalSec * 1000);
    return () => clearInterval(t);
  }, [loadSignal, intervalSec, paused]);

  // K 线入库实时状态轮询（慢轮询，6s；独立于 3s 信号轮询，降低 PG 压力）
  useEffect(() => {
    if (paused) return undefined;
    void loadKlineStatus(symbol);
    const t = setInterval(() => { void loadKlineStatus(symbol); }, 6000);
    return () => clearInterval(t);
  }, [symbol, paused, loadKlineStatus]);

  // ── AI 信号质量评分快照（AI评分/总分/外部因子评分三卡，慢轮询 6s）──
  const loadAiSnapshot = useCallback(async (sym: string): Promise<void> => {
    try {
      const res = await client.get(`${ENDPOINTS.hexp.ai}/${encodeURIComponent(sym)}`);
      const body = res.data;
      if (body?.code === 0) setAiSnap((body.data as HexpAiSnapshot) ?? null);
      else setAiSnap(null);
    } catch {
      setAiSnap(null);
    }
  }, []);

  useEffect(() => {
    if (paused) return undefined;
    void loadAiSnapshot(symbol);
    const t = setInterval(() => { void loadAiSnapshot(symbol); }, 6000);
    return () => clearInterval(t);
  }, [symbol, paused, loadAiSnapshot]);

  // 新鲜度显示心跳（1s）
  useEffect(() => {
    const t = setInterval(() => setTick((v) => v + 1), 1000);
    return () => clearInterval(t);
  }, []);

  const ageSec = useMemo(() => snapshotAgeSec(snap?.ts), [snap]);
  const notActive = activeModel !== null && activeModel !== 'hexp';

  return (
    <Box sx={{ p: 2, background: C.panelBg2, minHeight: '100%' }}>
      {/* 页头 */}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 1.5, flexWrap: 'wrap' }}>
        <Typography sx={{ fontSize: 18, fontWeight: 800, color: C.textMain }}>
          和乘幂信号状态
        </Typography>
        <Box
          sx={{
            px: 0.9, py: 0.2, borderRadius: 0.8, fontSize: 10.5, fontWeight: 700,
            color: C.violet, border: `1px solid ${C.violet}55`, background: `${C.violet}12`,
          }}
        >
          HP-Score 实时观测
        </Box>
        <Typography sx={{ fontSize: 11.5, color: C.textDim }}>
          {symbol} · 引擎约 3s 重算 · 只读页面，不影响下单
        </Typography>

        <Box sx={{ flex: 1 }} />

        {/* 轮询间隔 */}
        <ToggleButtonGroup
          size="small"
          exclusive
          value={intervalSec}
          onChange={(_, v) => { if (v) setIntervalSec(v as number); }}
          sx={{
            '& .MuiToggleButton-root': {
              fontSize: 10.5, py: 0.2, px: 1, color: C.textDim,
              borderColor: C.border, textTransform: 'none',
            },
            '& .Mui-selected': { color: `${C.info} !important`, background: `${C.info}18 !important` },
          }}
        >
          {INTERVALS.map((s) => (
            <ToggleButton key={s} value={s}>{s}s</ToggleButton>
          ))}
        </ToggleButtonGroup>

        <Box
          component="button"
          onClick={() => setPaused((p) => !p)}
          sx={{
            fontSize: 10.5, py: 0.35, px: 1.2, borderRadius: 1, cursor: 'pointer',
            color: paused ? C.warn : C.textDim, background: 'transparent',
            border: `1px solid ${paused ? `${C.warn}66` : C.border}`,
          }}
        >
          {paused ? '▶ 继续' : '⏸ 暂停'}
        </Box>
        <Box
          component="button"
          onClick={() => { void loadSignal(); void loadConfig(); void loadActiveModel(); }}
          sx={{
            fontSize: 10.5, py: 0.35, px: 1.2, borderRadius: 1, cursor: 'pointer',
            color: C.textDim, background: 'transparent', border: `1px solid ${C.border}`,
            '&:hover': { color: C.info, borderColor: `${C.info}66` },
          }}
        >
          ⟳ 刷新
        </Box>
      </Box>

      {/* 未激活提示 */}
      {notActive && (
        <Box
          sx={{
            mb: 1.5, px: 1.5, py: 1, borderRadius: 1.5,
            background: `${C.warn}12`, border: `1px solid ${C.warn}55`,
            display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap',
          }}
        >
          <Typography sx={{ fontSize: 12, color: C.warn, fontWeight: 700 }}>
            ⚠ 和乘幂当前未激活
          </Typography>
          <Typography sx={{ fontSize: 11.5, color: C.textDim }}>
            当前激活模型为 <b style={{ color: C.textMain }}>{activeModel}</b>（非和乘幂 HEXP）。
            本页数据可能为空或陈旧；如需激活，请到「信号模式与市况」页将激活模型切换为「和乘幂（HEXP）」。
          </Typography>
        </Box>
      )}

      {/* 错误 / 空数据提示 */}
      {error && !snap && (
        <Box
          sx={{
            mb: 1.5, px: 1.5, py: 1, borderRadius: 1.5,
            background: C.flatSoft, border: `1px solid ${C.border}`,
          }}
        >
          <Typography sx={{ fontSize: 11.5, color: C.textDim }}>
            {firstLoad ? '加载中…' : error}
          </Typography>
        </Box>
      )}

      {firstLoad && !snap ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 8 }}>
          <CircularProgress size={28} />
        </Box>
      ) : (
        <Box
          sx={{
            display: 'grid', gap: 2,
            gridTemplateColumns: { xs: '1fr', lg: 'repeat(12, 1fr)' },
          }}
        >
          {/* A · 核心决策 */}
          <Box sx={{ gridColumn: { xs: '1', lg: 'span 12' } }}>
            <DecisionPanel snap={snap} cfg={cfg} ageSec={ageSec} symbol={symbol} aiSnap={aiSnap} />
          </Box>

          {/* B · 和乘幂核心 */}
          <Box sx={{ gridColumn: { xs: '1', lg: 'span 4' } }}>
            <HpCorePanel snap={snap} cfg={cfg} />
          </Box>
          {/* C · 多周期共振 */}
          <Box sx={{ gridColumn: { xs: '1', lg: 'span 4' } }}>
            <ResonancePanel snap={snap} cfg={cfg} klineStatus={klineStatus} feedAgeSec={feedAgeSec} />
          </Box>
          {/* D · 评分卡 */}
          <Box sx={{ gridColumn: { xs: '1', lg: 'span 4' } }}>
            <ScorecardPanel snap={snap} cfg={cfg} />
          </Box>

          {/* E · 微结构动量与入场模式 */}
          <Box sx={{ gridColumn: { xs: '1', lg: 'span 6' } }}>
            <MicroPanel snap={snap} cfg={cfg} />
          </Box>
          {/* F · 执行预案 */}
          <Box sx={{ gridColumn: { xs: '1', lg: 'span 6' } }}>
            <ExecutionPanel snap={snap} cfg={cfg} />
          </Box>

          {/* G · 信号日志 */}
          <Box sx={{ gridColumn: { xs: '1', lg: 'span 12' } }}>
            <LogPanel
              logs={logs}
              onlyChanges={onlyChanges}
              onToggleMode={setOnlyChanges}
              onClear={() => setLogs([])}
            />
          </Box>
        </Box>
      )}

      {/* 图例 */}
      <Box sx={{ mt: 2, display: 'flex', gap: 2, flexWrap: 'wrap', alignItems: 'center' }}>
        <Typography sx={{ fontSize: 10, color: C.textFaint }}>图例：</Typography>
        {[
          { c: C.up, t: '做多 / 上行 / 正因子' },
          { c: C.down, t: '做空 / 下行 / 负因子' },
          { c: C.warn, t: '转换态 / 预警 / 阈值线' },
          { c: C.info, t: '中性量化数值' },
          { c: C.violet, t: '幂指数 k' },
        ].map((l) => (
          <Box key={l.t} sx={{ display: 'flex', alignItems: 'center', gap: 0.5 }}>
            <Box sx={{ width: 9, height: 9, borderRadius: '50%', background: l.c }} />
            <Typography sx={{ fontSize: 10, color: C.textFaint }}>{l.t}</Typography>
          </Box>
        ))}
        <Tooltip
          arrow
          title="入场模式（回踩 A / 突破 B）为看板启发式推断，引擎未输出该字段，不参与下单决策"
        >
          <Typography sx={{ fontSize: 10, color: C.warn, cursor: 'help' }}>
            ⚠ 「入场模式」为前端推断，非引擎输出
          </Typography>
        </Tooltip>
      </Box>
    </Box>
  );
};

export default HexpDashboard;
