import React, { useState, useEffect, useCallback } from 'react';
import { Box, Typography, Chip, LinearProgress, Button, Select, MenuItem } from '@mui/material';
import { RefreshCw, CheckCircle, Error, Warning, HourglassEmpty } from "../../components/Icons";
import ReactEChartsCore from 'echarts-for-react/lib/core';
import * as echarts from 'echarts/core';
import { LineChart } from 'echarts/charts';
import { GridComponent, TooltipComponent, LegendComponent, MarkLineComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import client from '../../api/client';
import { ENDPOINTS } from '../../api/endpoints';
import PipelineBar from '../../components/PipelineBar';
import KpiCard from '../../components/KpiCard';

echarts.use([LineChart, GridComponent, TooltipComponent, LegendComponent, MarkLineComponent, CanvasRenderer]);

interface ServiceHealth {
  name: string;
  status: 'healthy' | 'degraded' | 'down' | 'starting';
  latency_ms: number;
  uptime_percent: number;
  last_error: string | null;
  version: string;
  details: Record<string, string>;
}

interface BridgeStatus {
  login: string | null;
  account_id: number | null;
  role: string | null;
  server: string | null;
  terminal: string | null;
  pid: number | string | null;
  lock_pid: string | null;
  status: 'alive' | 'stale' | 'down' | 'duplicate' | string;
  age_seconds: number | null;
}

interface HealthSummary {
  klines_loaded: number;
  klines_by_symbol?: Record<string, number>;
  engine_rules: number;
  mt5_accounts: number;
  price_feed: string;
  ai_status: string;
  ai_signals_last_hour?: number;
  latest_pre_score?: number | null;
  score_threshold?: number;
  signal_tower_heartbeat?: string | null;
  klines_last_time: string | null;
  klines_stale_minutes: number | null;
  bridges?: BridgeStatus[];
}

const Health: React.FC = () => {
  const [services, setServices] = useState<ServiceHealth[]>([]);
  const [summary, setSummary] = useState<HealthSummary>({
    klines_loaded: 0,
    klines_by_symbol: {},
    engine_rules: 0,
    mt5_accounts: 0,
    price_feed: '--',
    ai_status: '无信号',
    ai_signals_last_hour: 0,
    latest_pre_score: null,
    score_threshold: 0.50,
    signal_tower_heartbeat: null,
    klines_last_time: null,
    klines_stale_minutes: null,
    bridges: [],
  });
  const [loading, setLoading] = useState<boolean>(false);
  const [lastUpdated, setLastUpdated] = useState<string>('');
  const [diagnosing, setDiagnosing] = useState<boolean>(false);
  const [diagnoseResult, setDiagnoseResult] = useState<{
    all_pass: boolean; nodes: { id: string; name: string; ok: boolean; msg: string; fix: string; healed?: boolean }[];
    suggestion: string; heal_applied: string[];
  } | null>(null);

  // 全备份状态
  const [backingUp, setBackingUp] = useState<boolean>(false);
  const [backupInfo, setBackupInfo] = useState<{
    status: string; zip?: string; size_mb?: number; error?: string;
    started_at?: string; finished_at?: string;
  } | null>(null);

  // 配置全量校准状态
  const [calibrating, setCalibrating] = useState<boolean>(false);
  const [calibrateResult, setCalibrateResult] = useState<{
    total: number; calibrated: number;
    drifts: { key: string; pg: string; redis: string | null }[];
    errors?: string[];
  } | null>(null);

  // AI 评分实时趋势（直观看涨跌 + veto/coupling 门槛）
  const [aiPoints, setAiPoints] = useState<{ t: string; ai: number | null; total: number | null }[]>([]);
  const [aiSymbol, setAiSymbol] = useState('XAUUSD');
  const [aiOnline, setAiOnline] = useState<boolean | null>(null);
  useEffect(() => {
    const timer = setInterval(() => {
      client.get(`${ENDPOINTS.hexp.ai}/${aiSymbol}`)
        .then((r) => {
          const d = r.data?.data ?? null;
          setAiOnline(!!d);
          if (!d) return;
          const ai = typeof d.ai_score === 'number' ? d.ai_score : null;
          const total = typeof d.total_score === 'number' ? d.total_score : null;
          setAiPoints((prev) => {
            const next = [...prev, {
              t: new Date().toLocaleTimeString('zh-CN', { hour12: false }),
              ai,
              total,
            }];
            return next.length > 120 ? next.slice(next.length - 120) : next;
          });
        })
        .catch(() => setAiOnline(false));
    }, 5000);
    return () => clearInterval(timer);
  }, [aiSymbol]);

  const backupLabel = (): string => {
    if (!backupInfo) return '';
    if (backupInfo.status === 'running') return '备份进行中…';
    if (backupInfo.status === 'done')
      return `备份完成 · ${backupInfo.zip ?? ''} (${backupInfo.size_mb ?? '?'} MB)`;
    if (backupInfo.status === 'failed') return `备份失败 · ${backupInfo.error ?? ''}`;
    return backupInfo.status;
  };

  const runBackup = useCallback(async () => {
    setBackingUp(true);
    setBackupInfo({ status: 'running' });
    try {
      await client.post(ENDPOINTS.dashboard.backup);
    } catch {
      // 触发失败也进入轮询，状态会从 Redis 反映真实情况
    }
    // 轮询 hcm:backup:status 直到完成/失败；idle(无消费者)视为失败，避免无限转圈
    const startedAt = Date.now();
    const timer = setInterval(async () => {
      try {
        const { data } = await client.get(ENDPOINTS.dashboard.backupStatus);
        const st = data?.data?.status;
        if (st === 'done' || st === 'failed') {
          setBackupInfo(data.data);
          setBackingUp(false);
          clearInterval(timer);
        } else if (st === 'running') {
          setBackupInfo(data.data);
        } else {
          // idle / no backup yet / 其它：trigger 无人消费，视为失败停止
          setBackupInfo({
            status: 'failed',
            error: '备份未执行：宿主桥(mt5_bridge)未运行或备份脚本缺失，请先启动 MT5 终端与桥',
          });
          setBackingUp(false);
          clearInterval(timer);
        }
      } catch {
        clearInterval(timer);
        setBackingUp(false);
      }
    }, 2000);
    // 90s 绝对超时保护：仍无 done/failed 则判定失败，停止轮询
    setTimeout(() => {
      clearInterval(timer);
      setBackingUp(false);
      setBackupInfo((prev) =>
        prev && (prev.status === 'running' || prev.status === 'idle')
          ? { status: 'failed', error: '备份超时（>90s 无响应），请检查宿主桥状态' }
          : prev ?? { status: 'failed', error: '备份超时' }
      );
    }, 90000);
  }, []);

  // 配置全量校准：以 PG 为权威，把全部漂移键覆盖写回 Redis。
  const runCalibrate = useCallback(async () => {
    setCalibrating(true);
    setCalibrateResult(null);
    try {
      const { data } = await client.post(ENDPOINTS.system.calibrateConfig, {});
      const body = data as {
        code: number;
        data?: { total: number; calibrated: number; drifts: { key: string; pg: string; redis: string | null }[]; errors?: string[] };
        message?: string;
      };
      if (body.code === 0 && body.data) {
        setCalibrateResult(body.data);
      } else {
        setCalibrateResult({ total: 0, calibrated: 0, drifts: [], errors: [body.message || '校准失败'] });
      }
    } catch (e: any) {
      setCalibrateResult({ total: 0, calibrated: 0, drifts: [], errors: [e?.message || '校准请求失败'] });
    } finally {
      setCalibrating(false);
    }
  }, []);

  const fetchHealth = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      const { data } = await client.get('/api/system/health/detailed');
      const payload = data.data || data;
      setServices(payload.services || []);
      setSummary({
        klines_loaded: payload.klines_loaded ?? 0,
        klines_by_symbol: payload.klines_by_symbol ?? {},
        engine_rules: payload.engine_rules ?? 0,
        mt5_accounts: payload.mt5_accounts ?? 0,
        price_feed: payload.price_feed ?? '--',
        ai_status: payload.ai_status ?? '无信号',
        ai_signals_last_hour: payload.ai_signals_last_hour ?? 0,
        latest_pre_score: payload.latest_pre_score ?? null,
        score_threshold: payload.score_threshold ?? 0.50,
        signal_tower_heartbeat: payload.signal_tower_heartbeat ?? null,
        klines_last_time: payload.klines_last_time ?? null,
        klines_stale_minutes: payload.klines_stale_minutes ?? null,
        bridges: payload.bridges ?? [],
      });
      setLastUpdated(new Date().toLocaleTimeString('zh-CN'));
    } catch {
      setServices([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const runDiagnose = useCallback(async (autoHeal: boolean = false) => {
    setDiagnosing(true);
    setDiagnoseResult(null);
    try {
      const { data } = await client.post(`${ENDPOINTS.system.diagnose}?auto_heal=${autoHeal}`);
      if (data.code === 0) setDiagnoseResult(data.data);
    } catch {
      // ignore
    } finally {
      setDiagnosing(false);
    }
  }, []);

  // ── 2026-09-02 AI 组件自愈（TimesFM + LightGBM sidecar + auto_retrain 守护）──
  // 容器无法启动 Windows 主机进程，本按钮只下发 Redis 信令；
  // 实际拉起由主机计划任务 HCM_AIStackGuard 每 3 分钟调用 ai_stack_guard.ps1 执行。
  const [aiHealing, setAiHealing] = useState(false);
  const [aiStatus, setAiStatus] = useState<Record<string, any> | null>(null);
  const [aiHealMsg, setAiHealMsg] = useState<string>('');

  const fetchAiStatus = useCallback(async () => {
    try {
      const { data } = await client.get(ENDPOINTS.system.aiStatus);
      if (data.code === 0) setAiStatus(data.data);
    } catch {
      // ignore
    }
  }, []);

  const runAiHeal = useCallback(async () => {
    setAiHealing(true);
    setAiHealMsg('');
    try {
      const { data } = await client.post(`${ENDPOINTS.system.aiHeal}?component=all`);
      setAiHealMsg(data.code === 0 ? (data.message || '已下发自愈信令')
                                   : (data.message || '自愈信令下发失败'));
    } catch {
      setAiHealMsg('自愈信令下发失败');
    } finally {
      setAiHealing(false);
      // 守护每 3 分钟巡检一轮，稍后回读状态
      setTimeout(fetchAiStatus, 8000);
    }
  }, [fetchAiStatus]);

  useEffect(() => {
    fetchHealth();
    fetchAiStatus();
    const interval = setInterval(() => { fetchHealth(); fetchAiStatus(); }, 15000);
    return () => clearInterval(interval);
  }, [fetchHealth, fetchAiStatus]);

  const statusIcon = (status: string): React.ReactNode => {
    switch (status) {
      case 'healthy': return <CheckCircle sx={{ color: '#22c55e', fontSize: 18 }} />;
      case 'degraded': return <Warning sx={{ color: '#eab308', fontSize: 18 }} />;
      case 'down': return <Error sx={{ color: '#ef4444', fontSize: 18 }} />;
      case 'starting': return <HourglassEmpty sx={{ color: '#3b82f6', fontSize: 18 }} />;
      default: return null;
    }
  };

  const statusLabel = (status: string): string => {
    switch (status) {
      case 'healthy': return '正常';
      case 'degraded': return '降级';
      case 'down': return '宕机';
      case 'starting': return '启动中';
      default: return status;
    }
  };

  const statusColor = (status: string): 'success' | 'warning' | 'error' | 'info' => {
    switch (status) {
      case 'healthy': return 'success';
      case 'degraded': return 'warning';
      case 'down': return 'error';
      default: return 'info';
    }
  };

  const overallHealth = (): string => {
    if (services.length === 0) return 'unknown';
    const allHealthy = services.every((s) => s.status === 'healthy');
    const anyDown = services.some((s) => s.status === 'down');
    if (allHealthy) return 'healthy';
    if (anyDown) return 'unhealthy';
    return 'degraded';
  };

  const overallColor = (): string => {
    switch (overallHealth()) {
      case 'healthy': return '#22c55e';
      case 'degraded': return '#eab308';
      case 'unhealthy': return '#ef4444';
      default: return '#64748b';
    }
  };

  const overallLabel = (): string => {
    switch (overallHealth()) {
      case 'healthy': return '系统正常';
      case 'degraded': return '部分降级';
      case 'unhealthy': return '系统异常';
      default: return '未知';
    }
  };

  // K-line staleness helper
  const klinesStaleness = (): { emoji: string; label: string; color: string } | null => {
    if (summary.klines_stale_minutes === null) return null;
    const m = summary.klines_stale_minutes;
    if (m < 5) return { emoji: '🟢', label: '实时', color: '#22c55e' };
    if (m < 30) return { emoji: '🟡', label: `延迟 ${m}分钟`, color: '#eab308' };
    return { emoji: '🔴', label: `停滞 ${m}分钟`, color: '#ef4444' };
  };

  // AI mode helpers
  const aiStatusColor = (status: string): string => {
    if (status.includes('AI')) return '#22c55e';
    if (status.includes('混合')) return '#a855f7';
    if (status.includes('指标')) return '#3b82f6';
    if (status.includes('无信号')) return '#64748b';
    return '#eab308';  // bypass / fallback
  };

  const aiStatusLabel = (status: string): string => {
    if (status === 'bypass') return '未启用';
    if (status === '无信号') return '等待信号';
    return status;
  };

  // Summary card definitions (K-line card rendered standalone below)
  const summaryCards: { label: string; value: string | number; color: string }[] = [
    { label: '推理规则', value: `${summary.engine_rules} 条`, color: '#8b5cf6' },
    { label: 'MT5 账户', value: `${summary.mt5_accounts} 个`, color: '#22c55e' },
  ];

  // Engine heartbeat staleness helper
  const heartbeat = summary.signal_tower_heartbeat;
  const engineStatus = (): { label: string; color: string } => {
    if (!heartbeat) return { label: '无心跳', color: '#ef4444' };
    const delta = (Date.now() - new Date(heartbeat).getTime()) / 60000;
    if (delta < 10) return { label: '正常', color: '#22c55e' };
    if (delta < 30) return { label: '延迟', color: '#eab308' };
    return { label: '宕机', color: '#ef4444' };
  };

  // Bridge liveness status meta
  const bridgeStatusMeta = (status: string): { label: string; color: string } => {
    switch (status) {
      case 'alive': return { label: '正常', color: '#22c55e' };
      case 'stale': return { label: '陈旧', color: '#eab308' };
      case 'duplicate': return { label: '重复实例', color: '#ef4444' };
      case 'down': return { label: '宕机', color: '#ef4444' };
      default: return { label: status, color: '#64748b' };
    }
  };

  return (
    <Box>
      <Box className="flex items-center justify-between mb-6">
        <Box>
          <Typography variant="h6" className="text-gray-100 font-semibold">系统健康</Typography>
          <Typography variant="caption" className="text-gray-500">
            最后更新: {lastUpdated || '--'}
          </Typography>
        </Box>
        <Box className="flex items-center gap-3">
          <Chip
            icon={<span className="w-2 h-2 rounded-full ml-1" style={{ backgroundColor: overallColor() }} />}
            label={overallLabel()}
            sx={{
              backgroundColor: `${overallColor()}22`,
              color: overallColor(),
              fontWeight: 600,
              border: `1px solid ${overallColor()}44`,
            }}
          />
          <Button variant="outlined" startIcon={<RefreshCw />} onClick={fetchHealth} disabled={loading}
            sx={{ borderColor: '#4b5563', color: '#94a3b8' }}>
            刷新
          </Button>
          <Button
            variant="contained"
            onClick={() => runDiagnose(false)}
            disabled={diagnosing}
            sx={{
              backgroundColor: diagnosing ? '#4b5563' : '#3b82f6',
              '&:hover': { backgroundColor: diagnosing ? '#4b5563' : '#2563eb' },
              fontWeight: 600,
            }}
          >
            {diagnosing ? '诊断中…' : '🔍 一键诊断'}
          </Button>
          <Button
            variant="contained"
            onClick={() => runDiagnose(true)}
            disabled={diagnosing}
            sx={{
              backgroundColor: diagnosing ? '#4b5563' : '#059669',
              '&:hover': { backgroundColor: diagnosing ? '#4b5563' : '#047857' },
              fontWeight: 600,
            }}
          >
            ⚡ 自愈
          </Button>
          <Button
            variant="contained"
            onClick={runAiHeal}
            disabled={aiHealing}
            title="下发自愈信令，由主机守护 HCM_AIStackGuard 拉起 TimesFM / LightGBM / auto_retrain 守护"
            sx={{
              backgroundColor: aiHealing ? '#4b5563' : '#0891b2',
              '&:hover': { backgroundColor: aiHealing ? '#4b5563' : '#0e7490' },
              fontWeight: 600,
            }}
          >
            {aiHealing ? '自愈中…' : '🧠 AI 自愈'}
          </Button>
          <Button
            variant="contained"
            onClick={runBackup}
            disabled={backingUp}
            sx={{
              backgroundColor: backingUp ? '#4b5563' : '#d97706',
              '&:hover': { backgroundColor: backingUp ? '#4b5563' : '#b45309' },
              fontWeight: 600,
            }}
          >
            {backingUp ? '备份中…' : '💾 全备份'}
          </Button>
          <Button
            variant="contained"
            onClick={runCalibrate}
            disabled={calibrating}
            sx={{
              backgroundColor: calibrating ? '#4b5563' : '#a855f7',
              '&:hover' : { backgroundColor: calibrating ? '#4b5563' : '#9333ea' },
              fontWeight: 600,
            }}
          >
            {calibrating ? '校准中…' : '🔧 校准配置'}
          </Button>
          {/* AI 组件存活状态（数据来自主机守护回写的 hcm:ai:{comp}:status） */}
          {aiStatus && (
            <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
              {[['lightgbm', 'LightGBM'], ['timesfm', 'TimesFM'], ['auto_retrain', '重训守护']].map(([k, label]) => {
                const st = (aiStatus as Record<string, any>)[k] || {};
                const alive = !!st.alive;
                return (
                  <Chip
                    key={k}
                    size="small"
                    label={`${label}: ${alive ? '存活' : (st.running ? '假死' : '失活')}`}
                    sx={{
                      backgroundColor: alive ? '#05966944' : '#ef444444',
                      color: alive ? '#34d399' : '#f87171',
                      fontSize: 11,
                    }}
                  />
                );
              })}
            </Box>
          )}
          {aiHealMsg && (
            <Chip size="small" label={aiHealMsg} sx={{ backgroundColor: '#0891b244', color: '#67e8f9', fontSize: 11 }} />
          )}
          {backupInfo && (
            <Chip
              size="small"
              label={backupLabel()}
              sx={{
                backgroundColor:
                  backupInfo.status === 'done' ? '#22c55e22'
                  : backupInfo.status === 'failed' ? '#ef444422'
                  : backupInfo.status === 'running' ? '#eab30822' : '#33415522',
                color:
                  backupInfo.status === 'done' ? '#34d399'
                  : backupInfo.status === 'failed' ? '#f87171'
                  : backupInfo.status === 'running' ? '#fbbf24' : '#94a3b8',
                fontSize: 11,
              }}
            />
          )}
        </Box>
      </Box>

      {/* ── Pipeline Axis Bar ── */}
      <PipelineBar />

      {/* ── Diagnose Result Panel ── */}
      {diagnoseResult && (
        <Box className="mb-6 p-4 rounded-lg border" sx={{
          borderColor: diagnoseResult.all_pass ? '#22c55e44' : '#ef444444',
          backgroundColor: diagnoseResult.all_pass ? '#22c55e08' : '#ef444408',
        }}>
          <Box className="flex items-center justify-between mb-3">
            <Typography variant="subtitle1" sx={{ color: diagnoseResult.all_pass ? '#22c55e' : '#ef4444', fontWeight: 600 }}>
              {diagnoseResult.all_pass ? '✅ 全链路畅通' : `⚠️ ${diagnoseResult.suggestion}`}
            </Typography>
            {diagnoseResult.heal_applied.length > 0 && (
              <Chip size="small" label={`已自愈 ${diagnoseResult.heal_applied.length} 项`} sx={{ backgroundColor: '#05966944', color: '#34d399' }} />
            )}
          </Box>
          <Box className="grid gap-2">
            {diagnoseResult.nodes.map((n) => (
              <Box key={n.id} className="flex items-center gap-3 px-3 py-2 rounded" sx={{
                backgroundColor: n.ok ? '#22c55e0a' : (n.healed ? '#eab3080a' : '#ef44440a'),
                borderLeft: `3px solid ${n.ok ? '#22c55e' : (n.healed ? '#eab308' : '#ef4444')}`,
              }}>
                <Typography variant="caption" sx={{ color: n.ok ? '#22c55e' : (n.healed ? '#eab308' : '#ef4444'), fontSize: 14 }}>
                  {n.ok ? '✅' : (n.healed ? '🔧' : '❌')}
                </Typography>
                <Typography variant="body2" sx={{ color: '#cbd5e1', minWidth: 80, fontWeight: 600 }}>
                  {n.name}
                </Typography>
                <Typography variant="caption" sx={{ color: '#94a3b8', flex: 1 }}>
                  {n.msg}
                </Typography>
                {n.healed ? (
                  <Chip size="small" label="已尝试自愈" sx={{ backgroundColor: '#eab30822', color: '#fbbf24', fontSize: 10 }} />
                ) : (!n.ok && n.fix && (
                  <Chip size="small" label={n.fix.replace(/_/g, ' ')} sx={{
                    backgroundColor: '#ef444422', color: '#f87171', fontSize: 10,
                  }} />
                ))}
              </Box>
            ))}
          </Box>
          {diagnoseResult.heal_applied.length > 0 && (
            <Box className="mt-3 pt-3 border-t" sx={{ borderColor: '#334155' }}>
              <Typography variant="caption" sx={{ color: '#34d399', fontWeight: 600 }}>⚡ 自愈执行记录</Typography>
              <Box className="grid gap-1 mt-1">
                {diagnoseResult.heal_applied.map((h, i) => (
                  <Typography key={i} variant="caption" sx={{ color: '#86efac', fontSize: 11 }}>
                    • {h}
                  </Typography>
                ))}
              </Box>
              <Typography variant="caption" sx={{ color: '#64748b', display: 'block', mt: 1, fontSize: 10 }}>
                注：容器 / PG / Redis 重启等需宿主机的动作不会自动执行，请按节点提示人工处理。
              </Typography>
            </Box>
          )}
        </Box>
      )}

      {/* ── Config Calibration Result ── */}
      {calibrateResult && (
        <Box className="mb-6 p-4 rounded-lg border" sx={{
          borderColor: (calibrateResult.errors && calibrateResult.errors.length)
            ? '#ef444444' : (calibrateResult.calibrated === 0 ? '#22c55e44' : '#a855f744'),
          backgroundColor: (calibrateResult.errors && calibrateResult.errors.length)
            ? '#ef444408' : (calibrateResult.calibrated === 0 ? '#22c55e08' : '#a855f708'),
        }}>
          <Box className="flex items-center justify-between mb-3">
            <Typography variant="subtitle1" sx={{
              color: (calibrateResult.errors && calibrateResult.errors.length)
                ? '#ef4444' : (calibrateResult.calibrated === 0 ? '#22c55e' : '#a855f7'),
              fontWeight: 600,
            }}>
              {calibrateResult.errors && calibrateResult.errors.length
                ? '❌ 校准失败'
                : (calibrateResult.calibrated === 0
                  ? `✅ 配置已全部一致（共 ${calibrateResult.total} 个键）`
                  : `🔧 已校准 ${calibrateResult.calibrated} 个漂移键（共 ${calibrateResult.total} 个）`)}
            </Typography>
          </Box>
          {calibrateResult.errors && calibrateResult.errors.length > 0 && (
            <Box className="grid gap-1">
              {calibrateResult.errors.map((e, i) => (
                <Typography key={i} variant="caption" sx={{ color: '#f87171', fontSize: 11 }}>• {e}</Typography>
              ))}
            </Box>
          )}
          {calibrateResult.drifts && calibrateResult.drifts.length > 0 && (
            <Box className="grid gap-1 mt-2">
              <Typography variant="caption" sx={{ color: '#c4b5fd', fontSize: 11, fontWeight: 600 }}>
                漂移明细（PG → Redis 已覆盖）
              </Typography>
              {calibrateResult.drifts.slice(0, 30).map((d, i) => (
                <Typography key={i} variant="caption" sx={{ color: '#94a3b8', fontSize: 11, fontFamily: 'monospace' }}>
                  • {d.key}: PG={d.pg ?? '∅'}  Redis={d.redis ?? '缺失'}
                </Typography>
              ))}
              {calibrateResult.drifts.length > 30 && (
                <Typography variant="caption" sx={{ color: '#64748b', fontSize: 10 }}>
                  …共 {calibrateResult.drifts.length} 条
                </Typography>
              )}
            </Box>
          )}
        </Box>
      )}

      {/* ── Summary Cards ── */}
      <Box className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-6">
        {/* K-line card — standalone with staleness indicator */}
        <Box
          className="card"
          sx={{
            borderLeft: `3px solid ${klinesStaleness()?.color ?? '#3b82f6'}`,
            backgroundColor: `${klinesStaleness()?.color ?? '#3b82f6'}0a`,
          }}
        >
          <Typography variant="caption" className="text-gray-500">K线数据</Typography>
          <Typography className="text-gray-200 font-semibold mt-1">
            已加载 {summary.klines_loaded.toLocaleString()} 根
          </Typography>
          {summary.klines_last_time && (
            <Typography variant="caption" className="text-gray-500" sx={{ display: 'block', mt: 0.5 }}>
              最近: {new Date(summary.klines_last_time).toLocaleString('zh-CN', {
                year: 'numeric', month: '2-digit', day: '2-digit',
                hour: '2-digit', minute: '2-digit',
              })}
            </Typography>
          )}
          {klinesStaleness() && (
            <Typography variant="caption" sx={{ display: 'block', mt: 0.25, color: klinesStaleness()!.color }}>
              {klinesStaleness()!.emoji} {klinesStaleness()!.label}
            </Typography>
          )}
        </Box>

        {summaryCards.map((card) => (
          <Box
            key={card.label}
            className="card"
            sx={{
              borderLeft: `3px solid ${card.color}`,
              backgroundColor: `${card.color}0a`,
            }}
          >
            <Typography variant="caption" className="text-gray-500">{card.label}</Typography>
            <Typography className="text-gray-200 font-semibold mt-1">
              {typeof card.value === 'number' ? card.value.toLocaleString() : card.value}
            </Typography>
          </Box>
        ))}

        {/* ── 推理评分卡 ── */}
        <KpiCard
          title="推理评分"
          color="#3b82f6"
          items={[
            { label: '指标评分', value: summary.ai_status === '指标评分' && summary.latest_pre_score != null ? summary.latest_pre_score.toFixed(3) : '—', color: summary.ai_status === '指标评分' ? '#3b82f6' : '#64748b' },
            { label: 'AI 评分', value: summary.ai_status === 'AI 评分' && summary.latest_pre_score != null ? summary.latest_pre_score.toFixed(3) : '—', color: summary.ai_status === 'AI 评分' ? '#22c55e' : '#64748b' },
            { label: '混合评分', value: summary.ai_status === '混合评分' && summary.latest_pre_score != null ? summary.latest_pre_score.toFixed(3) : '—', color: summary.ai_status === '混合评分' ? '#a855f7' : '#64748b' },
            { label: '引擎状态', value: engineStatus().label, color: engineStatus().color },
          ]}
        />
      </Box>

      {/* ── AI 评分实时趋势 ── */}
      <Box className="card mb-6" sx={{ p: 2 }}>
        <Box className="flex items-center justify-between mb-2">
          <Typography variant="subtitle2" className="text-gray-300 font-semibold">
            AI 评分实时趋势
          </Typography>
          <Box className="flex items-center gap-2">
            <Select
              size="small" value={aiSymbol}
              onChange={(e) => { setAiSymbol(e.target.value as string); setAiPoints([]); }}
              sx={{ minWidth: 130, height: 32, color: '#e2e8f0', '.MuiOutlinedInput-notchedOutline': { borderColor: '#334155' } }}
            >
              {['XAUUSD', 'XAGUSD', 'EURUSD', 'GBPUSD'].map((s) => <MenuItem key={s} value={s}>{s}</MenuItem>)}
            </Select>
            <Chip
              size="small"
              label={aiOnline === null ? '探测中…' : aiOnline ? 'sidecar 在线' : 'sidecar 离线'}
              sx={{
                backgroundColor: aiOnline === null ? '#33415522' : aiOnline ? '#22c55e22' : '#ef444422',
                color: aiOnline === null ? '#94a3b8' : aiOnline ? '#34d399' : '#f87171',
                fontSize: 11,
              }}
            />
          </Box>
        </Box>
        <Box sx={{ height: 200 }}>
          {aiPoints.length > 1 ? (
            <ReactEChartsCore
              echarts={echarts}
              notMerge
              style={{ height: 200 }}
              option={{
                tooltip: { trigger: 'axis' },
                legend: { data: ['ai_score', 'total_score'], textStyle: { color: '#94a3b8', fontSize: 11 } },
                grid: { left: 45, right: 15, top: 35, bottom: 40 },
                xAxis: { type: 'category', data: aiPoints.map((p) => p.t), axisLabel: { color: '#94a3b8', fontSize: 9 } },
                yAxis: { type: 'value', min: 0, max: 100, axisLabel: { color: '#94a3b8' }, splitLine: { lineStyle: { color: '#1e293b' } } },
                series: [
                  {
                    name: 'ai_score', type: 'line', data: aiPoints.map((p) => p.ai),
                    smooth: true, showSymbol: false, itemStyle: { color: '#22c55e' }, areaStyle: { opacity: 0.15 },
                    markLine: {
                      silent: true, symbol: 'none',
                      data: [
                        { yAxis: 30, lineStyle: { color: '#ef4444', type: 'dashed' }, label: { color: '#ef4444', formatter: 'veto 30' } },
                        { yAxis: 50, lineStyle: { color: '#eab308', type: 'dashed' }, label: { color: '#eab308', formatter: 'pass 50' } },
                      ],
                    },
                  },
                  {
                    name: 'total_score', type: 'line', data: aiPoints.map((p) => p.total),
                    smooth: true, showSymbol: false, itemStyle: { color: '#3b82f6' },
                  },
                ],
              }}
            />
          ) : (
            <Typography className="text-gray-500 text-sm" sx={{ textAlign: 'center', py: 6 }}>
              采集 AI 评分中…（每 5s 一个点）
            </Typography>
          )}
        </Box>
      </Box>

      {/* ── Bridge Liveness Panel ── */}
      <Box className="mb-6">
        <Typography variant="subtitle2" className="text-gray-300 font-semibold mb-2">
          桥存活监控
          <Typography variant="caption" className="text-gray-500 ml-2">
            数据来自 bridge:alive:&lt;login&gt; 心跳（每 ~10s 续期，换经纪商即插即用）
          </Typography>
        </Typography>
        <Box className="grid gap-2">
          {summary.bridges && summary.bridges.length > 0 ? (
            summary.bridges.map((b, i) => {
              const meta = bridgeStatusMeta(b.status);
              return (
                <Box
                  key={i}
                  className="flex items-center gap-3 px-3 py-2 rounded"
                  sx={{ backgroundColor: `${meta.color}0a`, borderLeft: `3px solid ${meta.color}` }}
                >
                  <Chip
                    size="small"
                    label={meta.label}
                    sx={{ backgroundColor: `${meta.color}22`, color: meta.color, fontSize: 10, minWidth: 64, height: 22 }}
                  />
                  <Typography variant="body2" className="text-gray-200 font-mono" sx={{ minWidth: 130 }}>
                    {b.login ?? `账户 ${b.account_id}`}
                  </Typography>
                  <Typography variant="caption" className="text-gray-400" sx={{ minWidth: 64 }}>
                    {b.role ?? '—'}
                  </Typography>
                  <Typography variant="caption" className="text-gray-400" sx={{ flex: 1 }}>
                    {b.server ?? '—'}
                  </Typography>
                  <Typography variant="caption" className="text-gray-500">
                    PID {b.pid ?? '—'}
                  </Typography>
                  <Typography variant="caption" className="text-gray-500">
                    {b.age_seconds != null ? `♥ ${b.age_seconds}s` : '—'}
                  </Typography>
                </Box>
              );
            })
          ) : (
            <Typography className="text-gray-500 text-sm py-2">未发现桥心跳（桥未运行或 Redis 不可读）</Typography>
          )}
        </Box>
      </Box>

      {/* ── K-line Staleness Diagnostic Panel ── */}
      {summary.klines_stale_minutes !== null && summary.klines_stale_minutes >= 30 && (
        <Box sx={{
          mt: 3, mb: 3, p: 2,
          border: '1px solid #eab30844',
          borderRadius: 2,
          backgroundColor: '#eab3080a',
        }}>
          <Typography variant="subtitle2" sx={{ color: '#eab308', fontWeight: 600, mb: 1 }}>
            K 线数据已 {summary.klines_stale_minutes} 分钟未更新 — 排查步骤
          </Typography>
          <Box component="ol" sx={{ pl: 2, m: 0, color: '#94a3b8', fontSize: '0.8rem', lineHeight: 1.8 }}>
            <li>确认 MT5 Bridge 是否在运行：检查宿主机 <code style={{backgroundColor:'#1a1a24',padding:'1px 6px',borderRadius:4}}>bridge.log</code></li>
            <li>确认 MT5 已登录且连接到模拟账户</li>
            <li>确认当前是否在交易时段（休市期间无新 K 线属正常）</li>
            <li>检查 hcm-collector 容器：<code style={{backgroundColor:'#1a1a24',padding:'1px 6px',borderRadius:4}}>docker logs hcm-v2-hcm-collector-1 --tail 50</code></li>
            <li>尝试重启采集器：<code style={{backgroundColor:'#1a1a24',padding:'1px 6px',borderRadius:4}}>docker restart hcm-v2-hcm-collector-1</code></li>
          </Box>
        </Box>
      )}

      {loading && services.length === 0 ? (
        <LinearProgress sx={{ backgroundColor: '#1a1a24', '& .MuiLinearProgress-bar': { backgroundColor: '#3b82f6' } }} />
      ) : services.length === 0 ? (
        <Typography className="text-gray-500 text-center py-12">无法获取健康状态</Typography>
      ) : (
        <Box className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {services.map((svc) => (
            <Box
              key={svc.name}
              className="card hover:border-gray-600 transition-colors"
              sx={{
                borderLeft: `3px solid ${
                  svc.status === 'healthy' ? '#22c55e' :
                  svc.status === 'degraded' ? '#eab308' :
                  svc.status === 'down' ? '#ef4444' : '#3b82f6'
                }`,
              }}
            >
              <Box className="flex items-center justify-between mb-3">
                <Box className="flex items-center gap-2">
                  {statusIcon(svc.status)}
                  <Typography className="font-semibold text-gray-200">{svc.name}</Typography>
                </Box>
                <Chip
                  label={statusLabel(svc.status)}
                  size="small"
                  color={statusColor(svc.status)}
                  sx={{ fontSize: 10, height: 20 }}
                />
              </Box>

              <Box className="space-y-2">
                <Box className="flex justify-between">
                  <Typography variant="caption" className="text-gray-500">延迟</Typography>
                  <Typography variant="caption" className="text-gray-300">{svc.latency_ms}ms</Typography>
                </Box>
                <Box className="flex justify-between">
                  <Typography variant="caption" className="text-gray-500">运行时间</Typography>
                  <Typography variant="caption" className="text-gray-300">{svc.uptime_percent.toFixed(2)}%</Typography>
                </Box>
                <Box className="flex justify-between">
                  <Typography variant="caption" className="text-gray-500">版本</Typography>
                  <Typography variant="caption" className="text-blue-400 font-mono">{svc.version}</Typography>
                </Box>
                {svc.last_error && (
                  <Box className="bg-red-900/20 border border-red-800/30 rounded-lg p-2 mt-2">
                    <Typography variant="caption" className="text-red-400">{svc.last_error}</Typography>
                  </Box>
                )}
                {Object.entries(svc.details).length > 0 && (
                  <Box className="mt-2 pt-2 border-t border-gray-700">
                    {Object.entries(svc.details).map(([key, val]) => (
                      <Box key={key} className="flex justify-between text-xs">
                        <span className="text-gray-500">{key}</span>
                        <span className="text-gray-400">{val}</span>
                      </Box>
                    ))}
                  </Box>
                )}
              </Box>
            </Box>
          ))}
        </Box>
      )}
    </Box>
  );
};

export default Health;
