import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Box, Paper, Typography, Chip, LinearProgress, ToggleButton, ToggleButtonGroup,
  Table, TableBody, TableCell, TableContainer, TableHead, TableRow, Alert, Collapse,
  IconButton, Tooltip,
} from '@mui/material';
import { ExpandMore, ExpandLess } from '@mui/icons-material';
import ReactEChartsCore from 'echarts-for-react/lib/core';
import * as echarts from 'echarts/core';
import { LineChart } from 'echarts/charts';
import { GridComponent, TooltipComponent, LegendComponent, MarkLineComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import client from '../../api/client';
import { ENDPOINTS } from '../../api/endpoints';
import { useSymbol } from '../../contexts/SymbolContext';

echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, MarkLineComponent, CanvasRenderer]);

/**
 * AI 中枢监控台（只读）
 * —— LightGBM 三头实时运作 + hexp 方向共振对照 + DeepSeek 工作效果
 *
 * 铁律合规要点：
 *  - 本页纯展示，**不写任何配置、不触发任何决策**。
 *  - 方向共振仅做「同向增强 / 反向否决」标注；hexp 无方向时明确标注
 *    「AI 不独立开仓」（铁律 5.1：AI 无独立开仓权）。
 *  - LightGBM 激活判定由后端读 ai.enabled + ai.mode（PG 真值），
 *    绝不依据幽灵键 ai.lm.enabled。
 */

const C = {
  bg: '#0d0d14', card: '#111118', inner: '#161622', border: '#2a2a3a', divider: '#1e293b',
  text: '#e2e8f0', sub: '#94a3b8', weak: '#64748b',
  ok: '#22c55e', warn: '#eab308', block: '#ef4444', idle: '#94a3b8', info: '#3b82f6', accent: '#a855f7',
};

/** 历史区时间窗（用户指定：1h / 24h / 7d 可调） */
const WINDOWS = [
  { label: '1 小时', hours: 1 },
  { label: '24 小时', hours: 24 },
  { label: '7 天', hours: 168 },
];

/** 实时轮询间隔（用户指定：10 秒） */
const POLL_MS = 10_000;

interface HeadState {
  value: string | number | null;
  prob?: number | null;
  enabled: boolean;
  label: string | null;
  // 价值头扩展字段（2026-09-05）：world=±1 方向世界 / 0 无趋势；score=顺向 E[R]
  world?: number | null;
  score?: number | null;
  reason?: string | null;
}
interface LiveData {
  symbol: string;
  lightgbm: {
    enabled: boolean; mode: string; active: boolean; status: string | null;
    valid: boolean | null; model_loaded: boolean | null; model_version: string | null;
    total_score: number | null; ext_factor_score: number | null; degrade_streak: number | null;
  };
  heads: {
    direction: HeadState; value: HeadState; entry: HeadState;
    quality: HeadState; state: HeadState;
  };
  hexp: {
    direction: string | null; grade: string | null; hp_score: number | null;
    verdict: number | null; scorecard_total: number | null; passed: boolean | null;
    close: number | null; atr: number | null;
  };
  resonance: { code: string; text: string; source?: string };
  deepseek: {
    key_ready: boolean; ticket: Record<string, unknown> | null; age_sec: number | null;
    max_age_sec: number; state: string; stale: boolean;
  };
  feature_health: {
    missing_ratio: number | null; constant_ratio: number | null; outlier_ratio: number | null;
    psi_drift: number | null; max_z: number | null; drift_level: string | null;
  };
  lm_features: Record<string, number> | null;
}

function fmt(v: number | null | undefined, d = 2): string {
  return v === null || v === undefined ? '—' : Number(v).toFixed(d);
}
function fmtAge(sec: number | null): string {
  if (sec === null) return '—';
  if (sec < 60) return `${Math.round(sec)}s`;
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  return `${(sec / 3600).toFixed(1)}h`;
}

/** 顶部 KPI 卡 */
const Kpi: React.FC<{
  title: string; value: string; sub?: string; tone?: string;
}> = ({ title, value, sub, tone }) => (
  <Paper elevation={0} sx={{
    backgroundColor: C.card, border: `1px solid ${C.border}`, borderRadius: 2, p: 1.5,
  }}>
    <Typography sx={{ color: C.sub, fontSize: 11 }}>{title}</Typography>
    <Typography sx={{ color: tone || C.text, fontWeight: 600, fontSize: 16, mt: 0.4 }}>{value}</Typography>
    {sub && <Typography sx={{ color: C.weak, fontSize: 10, mt: 0.3 }}>{sub}</Typography>}
  </Paper>
);

/** 单头卡片：值 + 阈值对照 + 结论徽章；未启用显示「未启用」 */
const HeadCard: React.FC<{
  title: string; head: HeadState | undefined; unit?: string; compare?: string;
  tone: string; badge: string; progress?: number | null;
}> = ({ title, head, unit, compare, tone, badge, progress }) => {
  const enabled = head?.enabled;
  return (
    <Box sx={{ backgroundColor: C.inner, border: `1px solid ${C.border}`, borderRadius: 1.5, p: 1.5 }}>
      <Typography sx={{ color: C.sub, fontSize: 11 }}>{title}</Typography>
      {!enabled ? (
        <Typography sx={{ color: C.idle, fontSize: 15, fontWeight: 600, mt: 1.2 }}>
          {head?.label || '未启用'}
        </Typography>
      ) : (
        <>
          <Typography sx={{ color: tone, fontSize: 20, fontWeight: 600, mt: 0.8 }}>
            {String(head?.value ?? '—')}{unit ? <Box component="span" sx={{ fontSize: 11, color: C.weak }}>{unit}</Box> : null}
          </Typography>
          {compare && <Typography sx={{ color: C.weak, fontSize: 10, mt: 0.2 }}>{compare}</Typography>}
          {progress !== null && progress !== undefined && (
            <LinearProgress variant="determinate" value={Math.max(0, Math.min(100, progress))}
              sx={{
                mt: 0.8, height: 3, borderRadius: 1.5, backgroundColor: C.divider,
                '& .MuiLinearProgress-bar': { backgroundColor: tone, borderRadius: 1.5 },
              }} />
          )}
        </>
      )}
      <Chip label={enabled ? badge : '未启用'} size="small" sx={{
        mt: 1, height: 20, fontSize: 10, fontWeight: 600,
        backgroundColor: enabled ? `${tone}22` : 'rgba(100,116,139,0.15)',
        color: enabled ? tone : C.idle,
      }} />
    </Box>
  );
};

export default function AiOpsConsole() {
  const { selectedSymbol } = useSymbol();
  const symbol = (selectedSymbol?.symbol || 'XAUUSD').toUpperCase();

  const [live, setLive] = useState<LiveData | null>(null);
  const [err, setErr] = useState('');
  const [hours, setHours] = useState(24);
  const [decisions, setDecisions] = useState<any>(null);
  const [dsStats, setDsStats] = useState<any>(null);
  const [selfHeal, setSelfHeal] = useState<any>(null); // ⑤ 自愈中心（阶段4）
  const [featOpen, setFeatOpen] = useState(false); // 特征面板默认折叠（用户指定）
  const timerRef = useRef<number | null>(null);

  const loadLive = useCallback(async () => {
    try {
      const r = await client.get(`${ENDPOINTS.ai.ops.live}/${symbol}`);
      setLive(r.data?.data ?? null);
      setErr('');
    } catch (e: any) {
      setErr(e?.response?.data?.message || e?.message || '实时数据读取失败');
    }
  }, [symbol]);

  const loadHistory = useCallback(async () => {
    try {
      const [dRes, sRes, shRes] = await Promise.all([
        client.get(ENDPOINTS.ai.ops.decisions, { params: { hours, limit: 200 } }),
        client.get(ENDPOINTS.ai.ops.dsStats, { params: { hours } }),
        client.get(ENDPOINTS.ai.ops.selfHeal, { params: { limit: 10 } }),
      ]);
      setDecisions(dRes.data?.data ?? null);
      setDsStats(sRes.data?.data ?? null);
      setSelfHeal(shRes.data?.data ?? null);
    } catch (e: any) {
      // 历史区失败不影响实时区
      setDecisions(null); setDsStats(null); setSelfHeal(null);
    }
  }, [hours]);

  // 实时区：10 秒轮询；页面不可见时暂停（省资源）
  useEffect(() => {
    loadLive();
    const start = () => {
      if (timerRef.current !== null) return;
      timerRef.current = window.setInterval(() => {
        if (document.visibilityState === 'visible') loadLive();
      }, POLL_MS);
    };
    const stop = () => {
      if (timerRef.current !== null) { window.clearInterval(timerRef.current); timerRef.current = null; }
    };
    start();
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') { loadLive(); start(); } else stop();
    });
    return () => { stop(); document.removeEventListener('visibilitychange', () => {}); };
  }, [loadLive]);

  useEffect(() => { loadHistory(); }, [loadHistory]);

  // DeepSeek fake_prob 时序图
  const dsChart = useMemo(() => {
    const series = dsStats?.series || [];
    return {
      grid: { left: 40, right: 16, top: 24, bottom: 28 },
      tooltip: { trigger: 'axis' as const },
      xAxis: {
        type: 'category' as const,
        data: series.map((s: any) => String(s.bucket).slice(5, 16)),
        axisLine: { lineStyle: { color: C.divider } },
        axisLabel: { color: C.weak, fontSize: 10 },
      },
      yAxis: {
        type: 'value' as const, min: 0, max: 1,
        axisLine: { lineStyle: { color: C.divider } },
        axisLabel: { color: C.weak, fontSize: 10 },
        splitLine: { lineStyle: { color: C.divider } },
      },
      series: [{
        name: '假信号概率',
        type: 'line' as const, smooth: true, symbolSize: 4,
        data: series.map((s: any) => s.avg_fake_prob),
        lineStyle: { color: C.info, width: 2 },
        itemStyle: { color: C.info },
        areaStyle: { color: 'rgba(59,130,246,0.12)' },
      }],
    };
  }, [dsStats]);

  const lgbm = live?.lightgbm;
  const ds = live?.deepseek;
  const res = live?.resonance;
  const hexp = live?.hexp;

  // 共振结论决定方向徽章（共振优先跟价值头世界方向比，见后端 _judge_resonance）
  const dirTone = res?.code === 'SAME' ? C.ok : res?.code === 'OPPOSITE' ? C.block : C.idle;
  const dirBadge = res?.code === 'SAME' ? '同向增强'
    : res?.code === 'OPPOSITE' ? '反向否决'
    : res?.code === 'HEXP_NO_DIRECTION' ? 'hexp 无方向'
    : 'AI 不干预';

  // 价值头（方向裁决源）：无趋势世界 / 未启用 → 灰；有方向 → 跟随共振结论着色
  const vh = live?.heads?.value;
  const valTone = !vh?.enabled || vh?.world === 0 ? C.idle : dirTone;
  const valCompare = !vh?.enabled
    ? '价值头未发布（world 缺失）'
    : `E[R] ${fmt(vh?.score, 3)}${vh?.world === 0 ? ' · 无趋势世界不评价值' : ''}`
      + (live?.heads?.direction?.enabled
        ? ` · dir_head 观测 ${String(live.heads.direction.value ?? '—')} 置信 ${fmt(live.heads.direction.prob, 2)}`
        : ' · dir_head 已停用');

  // DeepSeek 票状态：无票 / 陈旧（标黄）/ 新鲜
  const dsStateText = ds?.state === 'no_ticket' ? '无票'
    : ds?.state === 'stale' ? `陈旧 ${fmtAge(ds?.age_sec ?? null)}`
    : `新鲜 ${fmtAge(ds?.age_sec ?? null)}`;
  const dsTone = ds?.state === 'no_ticket' ? C.idle : ds?.state === 'stale' ? C.warn : C.ok;

  const psiColor = (v: number | null) => {
    if (v === null) return C.idle;
    if (v > 0.25) return C.block;
    if (v > 0.1) return C.warn;
    return C.ok;
  };

  return (
    <Box sx={{ p: 2, backgroundColor: C.bg, minHeight: '100%' }}>
      <Typography variant="h6" sx={{ color: C.text, fontWeight: 600, fontSize: 16 }}>AI 中枢监控</Typography>
      <Typography sx={{ color: C.weak, fontSize: 12, mb: 2 }}>
        LightGBM 三头运作 · DeepSeek 协作效果（只读，每 {POLL_MS / 1000} 秒刷新）
      </Typography>

      {err && <Alert severity="error" sx={{ mb: 2 }}>{err}</Alert>}

      {/* ── 顶部 KPI ── */}
      <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 1.5, mb: 2 }}>
        <Kpi
          title="LightGBM 状态"
          value={lgbm?.status || '—'}
          sub={lgbm ? `模式 ${lgbm.mode}${lgbm.active ? ' · 已激活' : ' · 未激活'}` : undefined}
          tone={lgbm?.status === 'ready' ? C.ok : lgbm?.status === 'degraded' ? C.warn : C.idle}
        />
        <Kpi title="模型版本" value={lgbm?.model_version || '—'} sub={`总分 ${fmt(lgbm?.total_score)}`} />
        <Kpi
          title="hexp 当前方向"
          value={hexp?.direction || '—'}
          sub={`档位 ${hexp?.grade || '—'} · 综合 ${fmt(hexp?.scorecard_total)}`}
          tone={hexp?.direction === 'BUY' ? C.ok : hexp?.direction === 'SELL' ? C.info : C.idle}
        />
        <Kpi title="DeepSeek 票" value={dsStateText} tone={dsTone}
          sub={`阈值 ${fmtAge(ds?.max_age_sec ?? null)} · key ${ds?.key_ready ? '就绪' : '未配置'}`} />
      </Box>

      {/* ── ① 三头实时运作 ── */}
      <Paper elevation={0} sx={{ backgroundColor: C.card, border: `1px solid ${C.border}`, borderRadius: 2, p: 2, mb: 2 }}>
        <Typography sx={{ color: C.accent, fontSize: 12, fontWeight: 600, mb: 1.2 }}>
          ① LightGBM 多头实时运作（价值头 = 方向裁决源，dir_head 降为观测）
        </Typography>
        <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 1.5 }}>
          <HeadCard
            title="价值头 value_head（方向裁决）"
            head={vh}
            compare={valCompare}
            tone={valTone}
            badge={!vh?.enabled ? '未启用'
              : vh?.world === 0 ? '无趋势·不评价值' : '顺向世界·裁决中'}
          />
          <HeadCard
            title="买点头 entry_head"
            head={live?.heads?.entry}
            unit=" 概率"
            compare="好买点概率（0~1）"
            tone={C.info} badge={live?.heads?.entry?.enabled ? '入场参考' : '未启用'}
            progress={live?.heads?.entry?.value != null ? Number(live.heads.entry.value) * 100 : null}
          />
          <HeadCard
            title="质量头 quality_head"
            head={live?.heads?.quality}
            compare={`总分 ${fmt(lgbm?.total_score)}`}
            tone={C.warn} badge={live?.heads?.quality?.enabled ? '质量评分' : '未启用'}
            progress={live?.heads?.quality?.value != null ? Number(live.heads.quality.value) : null}
          />
          <HeadCard
            title="状态头 state_head（仅观测）"
            head={live?.heads?.state}
            compare="不参与裁决"
            tone={C.idle} badge={live?.heads?.state?.enabled ? '观测中' : '未启用'}
          />
        </Box>

        {/* 方向共振对照 —— 铁律「AI 无独立开仓权」可视化 */}
        <Box sx={{
          mt: 1.5, p: 1.2, borderRadius: 1.5,
          backgroundColor: res?.code === 'OPPOSITE' ? 'rgba(239,68,68,0.10)'
            : res?.code === 'SAME' ? 'rgba(34,197,94,0.10)' : 'rgba(100,116,139,0.08)',
          border: `1px solid ${res?.code === 'OPPOSITE' ? C.block : res?.code === 'SAME' ? C.ok : C.border}`,
        }}>
          <Typography sx={{ color: C.text, fontSize: 12 }}>
            方向共振：<Box component="span" sx={{ color: dirTone, fontWeight: 600 }}>
              {res?.text || '—'}
            </Box>
            <Box component="span" sx={{ color: C.weak, fontSize: 11, ml: 1 }}>
              hexp={hexp?.direction || '—'} / {res?.source === 'value' ? '价值头' : 'dir_head'}
              ={String((res?.source === 'value' ? vh?.value : live?.heads?.direction?.value) ?? '—')}
            </Box>
          </Typography>
          <Typography sx={{ color: C.weak, fontSize: 10, mt: 0.3 }}>
            铁律 5.1：AI 仅有否决权与降/升级权，无独立开仓权 —— AI 方向只用于增强或否决 hexp，绝不自行开仓。
          </Typography>
        </Box>
      </Paper>

      {/* ── ② DeepSeek 工作效果 ── */}
      <Paper elevation={0} sx={{ backgroundColor: C.card, border: `1px solid ${C.border}`, borderRadius: 2, p: 2, mb: 2 }}>
        <Typography sx={{ color: C.accent, fontSize: 12, fontWeight: 600, mb: 0.4 }}>
          ② DeepSeek 工作效果
        </Typography>
        <Typography sx={{ color: C.weak, fontSize: 10, mb: 1.2 }}>
          2026-08-18 解耦后：DeepSeek 不参与运行期裁决，仅通过离线训练样本权重校准 LightGBM。
        </Typography>
        <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 1.5, mb: 1.5 }}>
          <Box sx={{ backgroundColor: C.inner, border: `1px solid ${C.border}`, borderRadius: 1.5, p: 1.2 }}>
            <Typography sx={{ color: C.sub, fontSize: 11 }}>实时假信号概率</Typography>
            <Typography sx={{ color: C.text, fontSize: 18, fontWeight: 600, mt: 0.5 }}>
              {ds?.ticket ? fmt(Number((ds.ticket as any).fake_prob), 3) : '—'}
            </Typography>
            <Typography sx={{ color: C.weak, fontSize: 10 }}>fake_prob</Typography>
          </Box>
          <Box sx={{ backgroundColor: C.inner, border: `1px solid ${C.border}`, borderRadius: 1.5, p: 1.2 }}>
            <Typography sx={{ color: C.sub, fontSize: 11 }}>趋势延续分</Typography>
            <Typography sx={{ color: C.text, fontSize: 18, fontWeight: 600, mt: 0.5 }}>
              {ds?.ticket ? String((ds.ticket as any).continuity_score ?? '—') : '—'}
            </Typography>
            <Typography sx={{ color: C.weak, fontSize: 10 }}>continuity</Typography>
          </Box>
          <Box sx={{ backgroundColor: C.inner, border: `1px solid ${C.border}`, borderRadius: 1.5, p: 1.2 }}>
            <Typography sx={{ color: C.sub, fontSize: 11 }}>调用次数 / 成功率</Typography>
            <Typography sx={{ color: C.text, fontSize: 18, fontWeight: 600, mt: 0.5 }}>
              {dsStats?.stats?.calls ?? '—'}
              <Box component="span" sx={{ fontSize: 12, color: C.weak, ml: 0.5 }}>
                / {dsStats?.stats?.success_rate != null ? `${(dsStats.stats.success_rate * 100).toFixed(1)}%` : '—'}
              </Box>
            </Typography>
            <Typography sx={{ color: C.weak, fontSize: 10 }}>近 {WINDOWS.find(w => w.hours === hours)?.label}</Typography>
          </Box>
          <Box sx={{
            backgroundColor: C.inner, borderRadius: 1.5, p: 1.2,
            border: `1px solid ${ds?.state === 'stale' ? C.warn : C.border}`,
          }}>
            <Typography sx={{ color: C.sub, fontSize: 11 }}>票新鲜度</Typography>
            <Typography sx={{ color: dsTone, fontSize: 18, fontWeight: 600, mt: 0.5 }}>{dsStateText}</Typography>
            <Typography sx={{ color: C.weak, fontSize: 10 }}>
              阈值 {fmtAge(ds?.max_age_sec ?? null)}
              {ds?.state === 'stale' ? ' · 已超期，不参与' : ''}
            </Typography>
          </Box>
        </Box>
        <ReactEChartsCore echarts={echarts} option={dsChart} style={{ height: 180 }} notMerge />
      </Paper>

      {/* ── 历史区时间窗切换（1h / 24h / 7d） ── */}
      <Box sx={{ display: 'flex', justifyContent: 'flex-end', mb: 1 }}>
        <ToggleButtonGroup size="small" exclusive value={hours}
          onChange={(_, v) => { if (v) setHours(v); }}>
          {WINDOWS.map(w => <ToggleButton key={w.hours} value={w.hours}
            sx={{ color: C.sub, '&.Mui-selected': { color: C.text, backgroundColor: 'rgba(168,85,247,0.18)' }, fontSize: 11 }}>
            {w.label}
          </ToggleButton>)}
        </ToggleButtonGroup>
      </Box>

      {/* ── ③ 裁决流水 ── */}
      <Paper elevation={0} sx={{ backgroundColor: C.card, border: `1px solid ${C.border}`, borderRadius: 2, p: 2, mb: 2 }}>
        <Typography sx={{ color: C.accent, fontSize: 12, fontWeight: 600, mb: 1 }}>
          ③ AI 闸门裁决流水
          <Box component="span" sx={{ color: C.weak, fontSize: 11, ml: 1, fontWeight: 400 }}>
            共 {decisions?.stats?.total ?? 0} 笔 · VETO {decisions?.stats?.veto ?? 0} ·
            降级 {decisions?.stats?.downgrade ?? 0} · 升级 {decisions?.stats?.upgrade ?? 0} ·
            保持 {decisions?.stats?.hold ?? 0}
          </Box>
        </Typography>
        <TableContainer sx={{ maxHeight: 320 }}>
          <Table size="small" stickyHeader>
            <TableHead>
              <TableRow>
                {['时间', '方向', '市况', '原档→终档', '裁决', 'AI 分', '手数档', '放行'].map(h => (
                  <TableCell key={h} sx={{ color: C.sub, fontSize: 11, backgroundColor: C.inner }}>{h}</TableCell>
                ))}
              </TableRow>
            </TableHead>
            <TableBody>
              {(decisions?.items || []).map((it: any) => {
                const tone = it.action === 'VETO' ? C.block
                  : it.action === 'DOWNGRADE' ? C.warn
                  : it.action === 'UPGRADE' ? C.ok : C.idle;
                return (
                  <TableRow key={it.decision_id} hover>
                    <TableCell sx={{ color: C.weak, fontSize: 11 }}>
                      {String(it.created_at || '').slice(5, 19).replace('T', ' ')}
                    </TableCell>
                    <TableCell sx={{ color: it.direction === 'BUY' ? C.ok : C.info, fontSize: 11 }}>{it.direction}</TableCell>
                    <TableCell sx={{ color: C.weak, fontSize: 11 }}>{it.regime || '—'}</TableCell>
                    <TableCell sx={{ color: C.text, fontSize: 11 }}>{it.orig_grade} → {it.final_grade}</TableCell>
                    <TableCell>
                      <Chip label={it.action} size="small" sx={{
                        height: 18, fontSize: 10, fontWeight: 600,
                        backgroundColor: `${tone}22`, color: tone,
                      }} />
                    </TableCell>
                    <TableCell sx={{ color: C.text, fontSize: 11 }}>{fmt(it.c_ai)}</TableCell>
                    <TableCell sx={{ color: C.weak, fontSize: 11 }}>{it.lot_tier || '—'}</TableCell>
                    <TableCell sx={{ color: it.passed ? C.ok : C.weak, fontSize: 11 }}>{it.passed ? '是' : '否'}</TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableContainer>
      </Paper>

      {/* ── ④ 特征健康（36 维面板默认折叠） ── */}
      <Paper elevation={0} sx={{ backgroundColor: C.card, border: `1px solid ${C.border}`, borderRadius: 2, p: 2 }}>
        <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <Typography sx={{ color: C.accent, fontSize: 12, fontWeight: 600 }}>
            ④ 特征健康
            <Box component="span" sx={{ color: C.weak, fontSize: 11, ml: 1, fontWeight: 400 }}>
              PSI {fmt(live?.feature_health?.psi_drift, 4)} · 漂移 {live?.feature_health?.drift_level || '—'} ·
              缺失 {(100 * (live?.feature_health?.missing_ratio ?? 0)).toFixed(0)}% ·
              恒值 {(100 * (live?.feature_health?.constant_ratio ?? 0)).toFixed(0)}% ·
              离群 {(100 * (live?.feature_health?.outlier_ratio ?? 0)).toFixed(1)}%
            </Box>
          </Typography>
          <Tooltip title={featOpen ? '收起 36 维特征' : '展开 36 维特征'}>
            <IconButton size="small" onClick={() => setFeatOpen(!featOpen)} sx={{ color: C.sub }}>
              {featOpen ? <ExpandLess /> : <ExpandMore />}
            </IconButton>
          </Tooltip>
        </Box>
        <LinearProgress variant="determinate"
          value={Math.min(100, (live?.feature_health?.psi_drift ?? 0) * 40)}
          sx={{
            mt: 1, height: 4, borderRadius: 2, backgroundColor: C.divider,
            '& .MuiLinearProgress-bar': {
              backgroundColor: psiColor(live?.feature_health?.psi_drift ?? null), borderRadius: 2,
            },
          }} />
        <Collapse in={featOpen} timeout="auto" unmountOnExit>
          <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', gap: 0.8, mt: 1.5 }}>
            {Object.entries(live?.lm_features || {}).map(([k, v]) => (
              <Box key={k} sx={{ backgroundColor: C.inner, borderRadius: 1, px: 1, py: 0.6 }}>
                <Typography noWrap title={k} sx={{ color: C.weak, fontSize: 9 }}>{k}</Typography>
                <Typography sx={{ color: C.text, fontSize: 11 }}>{fmt(Number(v), 4)}</Typography>
              </Box>
            ))}
          </Box>
        </Collapse>
      </Paper>

      {/* ── ⑤ 自愈中心（阶段4）：三头自愈闭环历史 + 健康状态 ── */}
      <Paper elevation={0} sx={{ backgroundColor: C.card, border: `1px solid ${C.border}`, borderRadius: 2, p: 2, mt: 2 }}>
        <Typography sx={{ color: C.accent, fontSize: 12, fontWeight: 600, mb: 1 }}>
          ⑤ 自愈中心
          <Box component="span" sx={{ color: C.weak, fontSize: 11, ml: 1, fontWeight: 400 }}>
            无干预复活闭环：PSI 漂移 / 定时重训 → 重训 → 回测 → 裁决 → 切换 → 热重载 → 自检 → 复活 / 回滚
          </Box>
        </Typography>

        {/* 守护 / 失败计数 / 最新轮 状态行 */}
        <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 1.5, mb: 1.5 }}>
          <Box sx={{ backgroundColor: C.inner, border: `1px solid ${C.border}`, borderRadius: 1.5, p: 1.2 }}>
            <Typography sx={{ color: C.sub, fontSize: 11 }}>自愈守护进程</Typography>
            <Typography sx={{
              color: selfHeal?.daemon ? C.ok : C.warn, fontSize: 18, fontWeight: 600, mt: 0.5,
            }}>
              {selfHeal?.daemon ? '心跳正常' : '未检测到心跳'}
            </Typography>
            <Typography sx={{ color: C.weak, fontSize: 10 }}>
              {selfHeal?.daemon?.at ? `更新 ${String(selfHeal.daemon.at).slice(5, 19).replace('T', ' ')}` : '守护未运行或心跳过期'}
            </Typography>
          </Box>
          <Box sx={{
            backgroundColor: C.inner, borderRadius: 1.5, p: 1.2,
            border: `1px solid ${selfHeal?.health?.fail_streak_alert ? C.block : C.border}`,
          }}>
            <Typography sx={{ color: C.sub, fontSize: 11 }}>连续复活失败计数</Typography>
            <Typography sx={{
              color: selfHeal?.health?.fail_streak_alert ? C.block : (selfHeal?.fail_streak ? C.warn : C.ok),
              fontSize: 18, fontWeight: 600, mt: 0.5,
            }}>
              {selfHeal?.fail_streak ?? 0}
              <Box component="span" sx={{ fontSize: 11, color: C.weak, ml: 0.5 }}>/ 3 轮告警</Box>
            </Typography>
            <Typography sx={{ color: C.weak, fontSize: 10 }}>
              {selfHeal?.health?.fail_streak_alert ? '已达告警阈值，需人工介入' : '切换成功自动清零'}
            </Typography>
          </Box>
          <Box sx={{ backgroundColor: C.inner, border: `1px solid ${C.border}`, borderRadius: 1.5, p: 1.2 }}>
            <Typography sx={{ color: C.sub, fontSize: 11 }}>最新一轮</Typography>
            <Typography sx={{ color: C.text, fontSize: 18, fontWeight: 600, mt: 0.5 }}>
              {selfHeal?.last?.model_version || '—'}
            </Typography>
            <Typography sx={{ color: C.weak, fontSize: 10 }}>
              {selfHeal?.last?.at ? String(selfHeal.last.at).slice(0, 16).replace('T', ' ') : '暂无记录'}
            </Typography>
          </Box>
          <Box sx={{ backgroundColor: C.inner, border: `1px solid ${C.border}`, borderRadius: 1.5, p: 1.2 }}>
            <Typography sx={{ color: C.sub, fontSize: 11 }}>最新一轮结果</Typography>
            <Typography sx={{ color: selfHeal?.health?.switched ? C.ok : C.warn, fontSize: 18, fontWeight: 600, mt: 0.5 }}>
              {selfHeal?.health?.switched ? '已切换' : selfHeal?.health?.adopted ? '已采纳' : '未切换'}
            </Typography>
            <Typography sx={{ color: C.weak, fontSize: 10 }}>
              {selfHeal?.last?.judge?.reason ? String(selfHeal.last.judge.reason).slice(0, 60) : '—'}
            </Typography>
          </Box>
        </Box>

        {/* 三头 + 校准健康条（阈值 0.55，用户决策1；单头退化=立即校准） */}
        <Box sx={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 1.5, mb: 1.5 }}>
          {([
            { k: 'quality', title: '质量头 AUC', val: selfHeal?.last?.auc, ok: selfHeal?.health?.quality_ok },
            { k: 'dir', title: '方向头 dir_hit', val: selfHeal?.last?.head_health?.dir_hit, ok: selfHeal?.health?.dir_ok },
            { k: 'entry', title: '买点头 entry AUC', val: selfHeal?.last?.head_health?.entry_auc, ok: selfHeal?.health?.entry_ok },
            { k: 'calib', title: '校准器退化', val: null, ok: !selfHeal?.health?.recalib_required },
          ] as any[]).map(h => (
            <Box key={h.k} sx={{
              backgroundColor: C.inner, borderRadius: 1.5, p: 1.2,
              border: `1px solid ${h.ok ? C.border : C.block}`,
            }}>
              <Typography sx={{ color: C.sub, fontSize: 11 }}>{h.title}</Typography>
              <Typography sx={{ color: h.ok ? C.ok : C.block, fontSize: 18, fontWeight: 600, mt: 0.5 }}>
                {h.val != null ? fmt(Number(h.val), 4) : (h.k === 'calib' ? (selfHeal?.health?.recalib_required ? '退化' : '健康') : '—')}
              </Typography>
              <Typography sx={{ color: C.weak, fontSize: 10 }}>
                {h.k === 'calib'
                  ? (selfHeal?.health?.recalib_required ? '需立即校准' : '无退化迹象')
                  : `阈值 ${fmt(selfHeal?.health?.threshold, 2)} ${h.ok ? '· 达标' : '· 不达标→校准'}`}
              </Typography>
            </Box>
          ))}
        </Box>

        {/* 重训历史（最近 N 轮） */}
        <TableContainer sx={{ maxHeight: 280 }}>
          <Table size="small" stickyHeader>
            <TableHead>
              <TableRow>
                {['时间', '版本', '质量 AUC', 'dir_hit', 'entry AUC', '样本', 'DS特征占比', '裁决', '切换'].map(h => (
                  <TableCell key={h} sx={{ color: C.sub, fontSize: 11, backgroundColor: C.inner }}>{h}</TableCell>
                ))}
              </TableRow>
            </TableHead>
            <TableBody>
              {(selfHeal?.rounds || []).map((r: any, i: number) => {
                const hh = r?.head_health || {};
                const adopted = r?.adopted || r?.judge?.decision === 'adopt';
                return (
                  <TableRow key={`${r?.at || i}-${r?.model_version || i}`} hover>
                    <TableCell sx={{ color: C.weak, fontSize: 11 }}>
                      {String(r?.at || '').slice(5, 19).replace('T', ' ')}
                    </TableCell>
                    <TableCell sx={{ color: C.text, fontSize: 11 }}>{r?.model_version || '—'}</TableCell>
                    <TableCell sx={{ color: (r?.auc ?? -1) >= (selfHeal?.health?.threshold ?? 0.55) ? C.ok : C.block, fontSize: 11 }}>
                      {fmt(r?.auc)}
                    </TableCell>
                    <TableCell sx={{ color: hh?.dir_hit != null && hh.dir_hit < (selfHeal?.health?.threshold ?? 0.55) ? C.block : C.text, fontSize: 11 }}>
                      {hh?.dir_hit != null ? fmt(hh.dir_hit, 4) : '—'}
                    </TableCell>
                    <TableCell sx={{ color: hh?.entry_auc != null && hh.entry_auc < (selfHeal?.health?.threshold ?? 0.55) ? C.block : C.text, fontSize: 11 }}>
                      {hh?.entry_auc != null ? fmt(hh.entry_auc, 4) : '—'}
                    </TableCell>
                    <TableCell sx={{ color: C.weak, fontSize: 11 }}>{r?.samples ?? '—'}</TableCell>
                    <TableCell sx={{ color: C.weak, fontSize: 11 }}>
                      {r?.ds_nonzero_ratio != null ? `${(r.ds_nonzero_ratio * 100).toFixed(0)}%` : '—'}
                    </TableCell>
                    <TableCell>
                      <Chip label={r?.judge?.decision || (adopted ? 'adopt' : 'reject')} size="small" sx={{
                        height: 18, fontSize: 10, fontWeight: 600,
                        backgroundColor: adopted ? 'rgba(34,197,94,0.18)' : 'rgba(100,116,139,0.15)',
                        color: adopted ? C.ok : C.idle,
                      }} />
                    </TableCell>
                    <TableCell sx={{ color: r?.switched ? C.ok : C.weak, fontSize: 11 }}>
                      {r?.switched ? '已切换' : (r?.dry_run ? 'dry-run' : '—')}
                    </TableCell>
                  </TableRow>
                );
              })}
              {!(selfHeal?.rounds?.length) && (
                <TableRow><TableCell colSpan={9} sx={{ color: C.weak, fontSize: 11, textAlign: 'center' }}>
                  暂无自愈历史（守护启动后每轮重训自动记录）
                </TableCell></TableRow>
              )}
            </TableBody>
          </Table>
        </TableContainer>
      </Paper>
    </Box>
  );
}
