import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert, Box, Chip, Paper, Table, TableBody, TableCell, TableContainer, TableHead,
  TableRow, ToggleButton, ToggleButtonGroup, Tooltip, Typography,
} from '@mui/material';
import client from '../../api/client';
import { ENDPOINTS } from '../../api/endpoints';
import { useSymbol } from '../../contexts/SymbolContext';
import { C } from '../state/dashboard/theme';

/**
 * AI 体检表（数据看板 · 只读）
 * =====================================================================
 * 数据来源：GET /api/v1/ai/health?symbol=&hours=&tf=（见 hcm-web/web/api/ai_health.py）
 *
 * 为什么单独做一页：2026-09-17 复盘发现三套模型的隐患**分散在 7 个 Redis 键 + 6 张表 +
 * 若干配置键**里（TimesFM 停用但仍写退化特征、v109 重训被硬性回滚、校准器样本不足三头
 * 全 skipped、状态模型 trend_init F1 仅 0.16），肉眼巡检成本极高 ⇒ 聚合成一次请求的体检表。
 *
 * 铁律合规：
 *  - 本页**纯展示**：不写配置、不触发任何决策、不干预下单链路。
 *  - 状态语义五档：正常 / 关注 / 异常 / 设计内停用 / 无数据源；**无数据源绝不显示为绿色**，
 *    后端每一行都带 `source`（数据源）与 `detail`（判据），本页原样呈现，不臆造结论。
 */

type Status = 'ok' | 'warn' | 'block' | 'idle' | 'na';

interface HealthRow {
  key: string; label: string; status: Status;
  value: string; detail?: string; source?: string;
}
interface HealthSection { key: string; label: string; status: Status; rows: HealthRow[] }
interface HealthReport {
  generated_at: string; symbol: string; time_frame: string; window_hours: number;
  overall: Status; summary: Partial<Record<Status, number>>;
  sections: HealthSection[]; elapsed_ms: number;
}

/** 五档状态的展示元数据（颜色沿用项目语义色：ok/warn/block/idle/weak） */
const ST: Record<Status, { t: string; c: string; b: string; hint: string }> = {
  ok: { t: '正常', c: C.ok, b: '#14532d', hint: '该检查项正常' },
  warn: { t: '关注', c: C.warn, b: '#713f12', hint: '不致命，但需人工确认/观察' },
  block: { t: '异常', c: C.block, b: '#7f1d1d', hint: '已影响信号质量，应尽快处理' },
  idle: { t: '停用', c: C.idle, b: '#334155', hint: '设计内停用/未启用（不是故障）' },
  na: { t: '无数据源', c: C.weak, b: '#334155', hint: '后端确实取不到该数据，不做臆测' },
};

const ORDER: Status[] = ['block', 'warn', 'ok', 'idle', 'na'];

const Pill = ({ s, big }: { s: Status; big?: boolean }) => {
  const m = ST[s] || ST.na;
  return (
    <Tooltip title={m.hint}>
      <Chip label={m.t} size="small" sx={{
        height: big ? 20 : 17, fontSize: big ? 11 : 9.5, fontWeight: 700,
        color: m.c, backgroundColor: 'transparent', border: `1px solid ${m.b}`,
        '& .MuiChip-label': { px: 0.8 },
      }} />
    </Tooltip>
  );
};

const Dot = ({ s }: { s: Status }) => (
  <Box component="span" sx={{
    display: 'inline-block', width: 7, height: 7, borderRadius: '50%',
    backgroundColor: (ST[s] || ST.na).c, mr: 0.7, verticalAlign: 'middle',
  }} />
);

export default function AiHealth() {
  const { selectedSymbol } = useSymbol();
  const symbol = (selectedSymbol?.symbol ?? 'XAUUSD').toUpperCase();

  const [report, setReport] = useState<HealthReport | null>(null);
  const [err, setErr] = useState('');
  const [lastTs, setLastTs] = useState('');
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [intervalSec, setIntervalSec] = useState(15);
  const [hours, setHours] = useState(48);
  const [tf, setTf] = useState('M5');
  const timerRef = useRef<number | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await client.get(ENDPOINTS.ai.health, { params: { symbol, hours, tf } });
      const env = (r?.data ?? {}) as { code: number; data: HealthReport | null; message?: string };
      if (env.code !== 0 || !env.data) {
        setErr(`体检接口返回异常：${env.message || env.code}`);
        return;
      }
      setReport(env.data);
      setErr('');
      setLastTs(new Date().toLocaleTimeString('zh-CN', { hour12: false }));
    } catch (e: any) {
      setErr(String(e?.message || e));
    }
  }, [symbol, hours, tf]);

  useEffect(() => { load(); }, [load]);

  /* 轮询：与系统健康页同范式（setInterval；页面不可见时暂停，避免后台空转） */
  useEffect(() => {
    if (timerRef.current) { window.clearInterval(timerRef.current); timerRef.current = null; }
    if (!autoRefresh) return undefined;
    const tick = () => { if (!document.hidden) load(); };
    timerRef.current = window.setInterval(tick, intervalSec * 1000);
    document.addEventListener('visibilitychange', tick);
    return () => {
      if (timerRef.current) window.clearInterval(timerRef.current);
      document.removeEventListener('visibilitychange', tick);
    };
  }, [autoRefresh, intervalSec, load]);

  /** 头部：只把「有内容」的档位显示出来，避免一排 0 噪音 */
  const summaryItems = useMemo(() => {
    const s = report?.summary || {};
    return ORDER.filter((k) => (s[k] || 0) > 0).map((k) => ({ k, n: s[k] as number }));
  }, [report]);

  const overall = report?.overall;
  const overallText = overall === 'block' ? '存在异常项 —— 需处理'
    : overall === 'warn' ? '存在关注项 —— 建议确认'
      : overall === 'ok' ? '全部正常' : '无可判定项';

  return (
    <Box sx={{ p: 2, backgroundColor: C.bg, minHeight: '100%' }}>
      {/* ── 头部：总体结论 + 统计 + 刷新控制 ───────────────────────────── */}
      <Paper elevation={0} sx={{ backgroundColor: C.card, border: `1px solid ${C.border}`, borderRadius: 2, p: 2, mb: 2 }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
          <Typography sx={{ color: C.text, fontSize: 15, fontWeight: 700 }}>AI 体检表</Typography>
          {overall && <Pill s={overall} big />}
          <Typography sx={{ color: overall === 'block' ? C.block : overall === 'warn' ? C.warn : C.sub, fontSize: 12 }}>
            {overallText}
          </Typography>
          <Box sx={{ ml: 'auto', display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
            <ToggleButtonGroup size="small" exclusive value={hours}
              onChange={(_, v) => v != null && setHours(v)}>
              {[24, 48, 72].map((h) => (
                <ToggleButton key={h} value={h} sx={{ px: 1.1, py: 0.1, fontSize: 10.5, color: C.sub }}>{h}h</ToggleButton>
              ))}
            </ToggleButtonGroup>
            <ToggleButtonGroup size="small" exclusive value={tf}
              onChange={(_, v) => v != null && setTf(v)}>
              {['M5', 'M15', 'H1'].map((t) => (
                <ToggleButton key={t} value={t} sx={{ px: 1.1, py: 0.1, fontSize: 10.5, color: C.sub }}>{t}</ToggleButton>
              ))}
            </ToggleButtonGroup>
            <ToggleButtonGroup size="small" exclusive value={intervalSec}
              onChange={(_, v) => v != null && setIntervalSec(v)}>
              {[10, 15, 30, 60].map((s) => (
                <ToggleButton key={s} value={s} sx={{ px: 1.1, py: 0.1, fontSize: 10.5, color: C.sub }}>{s}s</ToggleButton>
              ))}
            </ToggleButtonGroup>
            <Chip label={autoRefresh ? '自动刷新 ON' : '自动刷新 OFF'} size="small"
              onClick={() => setAutoRefresh((v) => !v)}
              sx={{ height: 20, fontSize: 10.5, color: autoRefresh ? C.ok : C.weak,
                    backgroundColor: 'transparent', border: `1px solid ${autoRefresh ? '#14532d' : C.border}`,
                    '& .MuiChip-label': { px: 0.9 } }} />
            <Chip label="立即刷新" size="small" onClick={load}
              sx={{ height: 20, fontSize: 10.5, color: C.info, backgroundColor: 'transparent',
                    border: `1px solid #1e3a8a`, '& .MuiChip-label': { px: 0.9 } }} />
          </Box>
        </Box>

        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.4, mt: 1, flexWrap: 'wrap', fontSize: 11 }}>
          {summaryItems.map(({ k, n }) => (
            <Box key={k} sx={{ display: 'flex', alignItems: 'center' }} title={ST[k].hint}>
              <Dot s={k} />
              <Box component="span" sx={{ color: ST[k].c, fontWeight: 700 }}>{ST[k].t}</Box>
              <Box component="span" sx={{ color: C.sub, ml: 0.4 }}>{n} 项</Box>
            </Box>
          ))}
          <Box sx={{ color: C.weak, ml: 'auto' }}>
            {symbol} · {tf} · 窗口 {report?.window_hours ?? hours}h · 快照 {report?.generated_at?.slice(11, 19) || '—'}
            {report?.elapsed_ms != null ? ` · 生成 ${report.elapsed_ms}ms` : ''}
            {lastTs ? ` · 本页 ${lastTs} 刷新` : ''}
          </Box>
        </Box>
        <Typography sx={{ color: C.weak, fontSize: 10.5, mt: 0.8 }}>
          口径说明：本页只读，不写配置、不触发决策。每一行都标「数据源」与「判据」；
          <Box component="span" sx={{ color: C.weak, fontWeight: 700 }}>无数据源一律显示为灰色「无数据源」，不假绿</Box>。
        </Typography>
      </Paper>

      {err && <Alert severity="warning" sx={{ mb: 2, backgroundColor: C.inner, color: C.warn }}>{err}</Alert>}

      {/* ── 三块体检区段 ─────────────────────────────────────────────── */}
      {(report?.sections || []).map((sec) => (
        <Paper key={sec.key} elevation={0}
          sx={{ backgroundColor: C.card, border: `1px solid ${C.border}`, borderRadius: 2, p: 2, mb: 2 }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1.2 }}>
            <Typography sx={{ color: C.text, fontSize: 13, fontWeight: 700 }}>{sec.label}</Typography>
            <Pill s={sec.status} />
            <Typography sx={{ color: C.weak, fontSize: 10.5, ml: 'auto' }}>
              {sec.rows.length} 项检查
            </Typography>
          </Box>

          <TableContainer>
            <Table size="small" stickyHeader>
              <TableHead>
                <TableRow>
                  {['检查项', '状态', '当前值', '判据 / 说明', '数据源'].map((h) => (
                    <TableCell key={h} sx={{
                      backgroundColor: C.inner, color: C.sub, fontSize: 10.5,
                      borderBottom: `1px solid ${C.border}`, py: 0.5, whiteSpace: 'nowrap',
                    }}>{h}</TableCell>
                  ))}
                </TableRow>
              </TableHead>
              <TableBody>
                {sec.rows.map((r) => (
                  <TableRow key={r.key} sx={{ '&:hover': { backgroundColor: C.inner } }}>
                    <TableCell sx={{ color: C.text, fontSize: 11.5, fontWeight: 600,
                                     borderBottom: `1px solid ${C.divider}`, py: 0.6, whiteSpace: 'nowrap' }}>
                      <Dot s={r.status} />{r.label}
                    </TableCell>
                    <TableCell sx={{ borderBottom: `1px solid ${C.divider}`, py: 0.6 }}>
                      <Pill s={r.status} />
                    </TableCell>
                    <TableCell sx={{ color: (ST[r.status] || ST.na).c, fontSize: 11.5, fontWeight: 700,
                                     borderBottom: `1px solid ${C.divider}`, py: 0.6,
                                     fontVariantNumeric: 'tabular-nums' }}>
                      {r.value}
                    </TableCell>
                    <TableCell sx={{ color: C.sub, fontSize: 10.5, maxWidth: 560,
                                     borderBottom: `1px solid ${C.divider}`, py: 0.6 }}>
                      {r.detail || '—'}
                    </TableCell>
                    <TableCell sx={{ color: C.weak, fontSize: 10, fontFamily: 'monospace',
                                     borderBottom: `1px solid ${C.divider}`, py: 0.6 }}>
                      {r.source || '—'}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableContainer>
        </Paper>
      ))}

      {!report && !err && (
        <Typography sx={{ color: C.weak, fontSize: 12 }}>加载中…</Typography>
      )}
    </Box>
  );
}
