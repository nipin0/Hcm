import React, { useState, useEffect, useCallback, useMemo } from 'react';
import { Box, Typography, Chip, LinearProgress } from '@mui/material';
import client from '../../api/client';
import { useSymbol } from '../../contexts/SymbolContext';
import KpiCard from '../../components/KpiCard';

/** Raw indicator key/value map for a single factor sub-item. */
interface FactorIndicators {
  dxy?: number;
  real_yield_10y?: number;
  real_yield?: number;
  fed_funds_rate?: number;
  cpi_yoy?: number;
  gold_etf_holdings?: number;
  comex_gold_oi?: number;
  central_bank_buying?: number;
  sentiment_risk_score?: number;
  vix?: number;
  risk_score?: number;
  [key: string]: number | undefined;
}

/** A single factor sub-item, e.g. data.macro.metals. */
interface FactorSubItem {
  category?: string;
  score?: number;
  bias?: 'bullish' | 'bearish' | 'neutral' | string;
  summary?: string;
  indicators?: FactorIndicators;
  snapshot_id?: number;
  collected_at?: string;
  source?: string;
  [key: string]: unknown;
}

/** Group containers for macro / sentiment factors. */
interface MacroSentimentGroup {
  metals?: FactorSubItem;
  crypto?: FactorSubItem;
  forex?: FactorSubItem;
  [key: string]: FactorSubItem | undefined;
}

/** Collector status reported by the backend (hcm:market:* flags). */
interface FactorStatus {
  stub_mode?: boolean | null;
  ai_offline?: boolean | null;
  ai_source?: 'deepseek' | 'heuristic' | 'stub' | 'unknown' | string;
  last_collection?: string | null;
  composite?: number | null;
  fresh?: boolean | null;
}

/** Full payload of the external-factors endpoint (data.data). */
interface ExternalFactorsData {
  macro?: MacroSentimentGroup;
  sentiment?: MacroSentimentGroup;
  event?: Record<string, unknown>;
  liquidity?: Record<string, unknown>;
  status?: FactorStatus;
}

/** Backend envelope: { code, data, message }. */
interface ExternalFactorsResponse {
  code: number;
  data: ExternalFactorsData;
  message: string;
}

type FactorGroupName = 'macro' | 'sentiment';
type FactorSubName = 'metals' | 'crypto' | 'forex';

const GROUPS: FactorGroupName[] = ['macro', 'sentiment'];
const SUBS: FactorSubName[] = ['metals', 'crypto', 'forex'];

const GROUP_LABELS: Record<FactorGroupName, string> = {
  macro: '宏观',
  sentiment: '情绪',
};

const SUB_LABELS: Record<FactorSubName, string> = {
  metals: '贵金属',
  crypto: '加密货币',
  forex: '外汇',
};

const BIAS_LABELS: Record<string, string> = {
  bullish: '看涨',
  bearish: '看跌',
  neutral: '中性',
};

/** Known indicator keys we want to surface, with Chinese labels. */
const INDICATOR_LABELS: Record<string, string> = {
  dxy: '美元指数',
  real_yield_10y: '10Y实际收益率',
  real_yield: '实际收益率',
  fed_funds_rate: '联邦基金利率',
  cpi_yoy: 'CPI同比',
  gold_etf_holdings: '黄金ETF持仓',
  comex_gold_oi: 'COMEX未平仓',
  central_bank_buying: '央行购金',
  sentiment_risk_score: '情绪风险分',
  vix: 'VIX恐慌指数',
  risk_score: '风险分',
};

/** Score color thresholds: >=60 green, >=30 yellow, else red. */
const scoreColor = (score: number): string => {
  if (score >= 60) return '#22c55e';
  if (score >= 30) return '#eab308';
  return '#ef4444';
};

const scoreBg = (score: number): string => {
  if (score >= 60) return '#0a2e1a';
  if (score >= 30) return '#3b2f0a';
  return '#3b1111';
};

const biasColor = (bias?: string): string => {
  switch (bias) {
    case 'bullish': return '#22c55e';
    case 'bearish': return '#ef4444';
    default: return '#94a3b8';
  }
};

const biasBg = (bias?: string): string => {
  switch (bias) {
    case 'bullish': return '#0a2e1a';
    case 'bearish': return '#3b1111';
    default: return '#1f2937';
  }
};

const fmtNum = (n: number): string => {
  if (!Number.isFinite(n)) return String(n);
  return n.toLocaleString('en-US', {
    maximumFractionDigits: Number.isInteger(n) ? 0 : 2,
  });
};

/** Format an ISO timestamp to "YYYY-MM-DD HH:mm" in UTC. */
const fmtTime = (iso?: string): string => {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (x: number): string => String(x).padStart(2, '0');
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
};

const stringifyValue = (value: unknown): string => {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
};

const Factors: React.FC = () => {
  const { selectedSymbol } = useSymbol();
  const [factorData, setFactorData] = useState<ExternalFactorsData | null>(null);
  const [loading, setLoading] = useState<boolean>(false);

  const fetchFactors = useCallback(async (): Promise<void> => {
    setLoading(true);
    try {
      // v2 native endpoint — no symbol parameter required.
      const { data } = await client.get<ExternalFactorsResponse>(
        '/api/v1/dashboard/external-factors',
      );
      if (data && data.code === 0 && data.data) {
        setFactorData(data.data);
      } else {
        setFactorData(null);
      }
    } catch {
      // Network/HTTP error (e.g. 404) — fall back to empty state, do not crash.
      setFactorData(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchFactors();
    // Lightweight live refresh; the endpoint ignores symbol so no dependency needed.
    const interval = setInterval(fetchFactors, 30000);
    return () => clearInterval(interval);
  }, [fetchFactors]);

  /** Flatten macro/sentiment into a renderable list. */
  const factorItems = useMemo(() => {
    if (!factorData) return [];
    const list: { group: FactorGroupName; sub: FactorSubName; item: FactorSubItem }[] = [];
    for (const g of GROUPS) {
      const grp = factorData[g];
      if (!grp) continue;
      for (const s of SUBS) {
        const it = grp[s];
        if (it) list.push({ group: g, sub: s, item: it });
      }
    }
    return list;
  }, [factorData]);

  const hasFactors = factorItems.length > 0;
  const eventEntries = useMemo(
    () => Object.entries(factorData?.event ?? {}).filter(([, v]) => v !== undefined && v !== null),
    [factorData],
  );
  const liquidityEntries = useMemo(
    () => Object.entries(factorData?.liquidity ?? {}).filter(([, v]) => v !== undefined && v !== null),
    [factorData],
  );
  const hasGeneric = eventEntries.length > 0 || liquidityEntries.length > 0;
  const isEmpty = !loading && !hasFactors && !hasGeneric;

  /** Derive an AI-status chip from the backend status block. */
  const statusInfo = useMemo(() => {
    const st = factorData?.status;
    if (!st) return null;
    const sourceMap: Record<string, { label: string; color: string; bg: string }> = {
      deepseek: { label: 'DeepSeek 实时 AI 评分', color: '#22c55e', bg: '#0a2e1a' },
      heuristic: { label: '启发式降级（AI 离线）', color: '#eab308', bg: '#3b2f0a' },
      stub: { label: '占位数据（STUB 模式）', color: '#ef4444', bg: '#3b1111' },
      unknown: { label: '来源未知', color: '#94a3b8', bg: '#1f2937' },
    };
    const info = sourceMap[st.ai_source ?? 'unknown'] ?? sourceMap.unknown;
    return { ...info, fresh: st.fresh, last: st.last_collection, composite: st.composite };
  }, [factorData]);

  if (loading && !factorData) {
    return (
      <LinearProgress
        sx={{ backgroundColor: '#1a1a24', '& .MuiLinearProgress-bar': { backgroundColor: '#3b82f6' } }}
      />
    );
  }

  return (
    <Box>
      <Box className="flex items-baseline gap-2 mb-4">
        <Typography variant="h6" className="text-gray-100 font-semibold">外部因子监控</Typography>
        {selectedSymbol && (
          <Typography variant="caption" className="text-gray-500">
            {selectedSymbol.symbol}
          </Typography>
        )}
      </Box>

      {/* AI scoring status strip — reflects whether the collector is using the
          real DeepSeek key (live) or has fallen back to heuristics/stub. */}
      {statusInfo && (
        <Box className="flex flex-wrap items-center gap-2 mb-4">
          <Chip
            label={statusInfo.label}
            size="small"
            sx={{
              backgroundColor: statusInfo.bg,
              color: statusInfo.color,
              fontWeight: 600,
            }}
          />
          {statusInfo.composite !== undefined && statusInfo.composite !== null && (
            <Chip
              label={`综合: ${(statusInfo.composite * 100).toFixed(0)}`}
              size="small"
              variant="outlined"
              sx={{ borderColor: '#334155', color: '#cbd5e1' }}
            />
          )}
          {statusInfo.fresh === false && (
            <Chip
              label="采集停滞(>48h)"
              size="small"
              sx={{ backgroundColor: '#3b1111', color: '#ef4444', fontWeight: 600 }}
            />
          )}
          {statusInfo.last && (
            <Typography variant="caption" className="text-gray-500">
              最后采集: {fmtTime(statusInfo.last)}
            </Typography>
          )}
        </Box>
      )}

      {isEmpty ? (
        <Typography className="text-gray-500 text-center py-8">暂无因子数据</Typography>
      ) : (
        <>
          {/* Overview KPI tiles — quick glance at each factor score. */}
          <Box className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
            {factorItems.map(({ group, sub, item }) => {
              const score = item.score ?? 0;
              const biasLabel = item.bias ? (BIAS_LABELS[item.bias] ?? item.bias) : '';
              return (
                <KpiCard
                  key={`${group}-${sub}`}
                  title={`${GROUP_LABELS[group]}·${SUB_LABELS[sub]}`}
                  value={score}
                  subtitle={biasLabel ? `倾向: ${biasLabel}` : '倾向: —'}
                  color={scoreColor(score)}
                />
              );
            })}
          </Box>

          {/* Detailed factor cards. */}
          <Box className="grid grid-cols-1 md:grid-cols-2 gap-3 mb-6">
            {factorItems.map(({ group, sub, item }) => {
              const score = item.score ?? 0;
              const indicators = item.indicators ?? {};
              const knownKeys = Object.keys(INDICATOR_LABELS).filter(
                (k) => typeof indicators[k] === 'number',
              );
              return (
                <Box key={`${group}-${sub}`} className="card p-4">
                  <Box className="flex items-center justify-between mb-2">
                    <Typography className="text-sm font-semibold text-gray-200">
                      {GROUP_LABELS[group]}·{SUB_LABELS[sub]}
                    </Typography>
                    {item.bias && (
                      <Chip
                        label={BIAS_LABELS[item.bias] ?? item.bias}
                        size="small"
                        sx={{
                          backgroundColor: biasBg(item.bias),
                          color: biasColor(item.bias),
                          fontWeight: 600,
                        }}
                      />
                    )}
                  </Box>

                  {/* Score progress bar (replaces the old echarts history chart). */}
                  <Box className="flex items-center gap-2 mb-2">
                    <LinearProgress
                      variant="determinate"
                      value={Math.max(0, Math.min(100, score))}
                      sx={{
                        flex: 1,
                        height: 6,
                        borderRadius: 3,
                        backgroundColor: '#1f2937',
                        '& .MuiLinearProgress-bar': { backgroundColor: scoreColor(score) },
                      }}
                    />
                    <Typography
                      variant="caption"
                      sx={{ color: scoreColor(score), fontWeight: 700, minWidth: 28, textAlign: 'right' }}
                    >
                      {score}
                    </Typography>
                  </Box>

                  {item.summary && (
                    <Typography variant="caption" className="text-gray-400 block mb-2 leading-relaxed">
                      {item.summary}
                    </Typography>
                  )}

                  {knownKeys.length > 0 && (
                    <Box className="flex flex-wrap gap-x-4 gap-y-1 mb-2">
                      {knownKeys.map((k) => (
                        <Typography key={k} variant="caption" className="text-gray-400">
                          {INDICATOR_LABELS[k]}:{' '}
                          <span className="text-gray-200">{fmtNum(indicators[k] as number)}</span>
                        </Typography>
                      ))}
                    </Box>
                  )}

                  <Box className="flex items-center gap-3 text-gray-600">
                    <Typography variant="caption">来源: {item.source ?? '—'}</Typography>
                    <Typography variant="caption">采集: {fmtTime(item.collected_at)}</Typography>
                  </Box>
                </Box>
              );
            })}
          </Box>

          {/* Defensive rendering for event / liquidity (variable structure). */}
          {eventEntries.length > 0 && (
            <Box className="card p-4 mb-4">
              <Typography variant="caption" className="text-gray-400 uppercase tracking-wider block mb-3">
                事件因子
              </Typography>
              <Box className="space-y-1">
                {eventEntries.map(([k, v]) => (
                  <Box key={k} className="flex items-start gap-2">
                    <Typography variant="caption" className="text-gray-500 min-w-[120px]">{k}</Typography>
                    <Typography variant="caption" className="text-gray-300 break-all">
                      {stringifyValue(v)}
                    </Typography>
                  </Box>
                ))}
              </Box>
            </Box>
          )}

          {liquidityEntries.length > 0 && (
            <Box className="card p-4 mb-4">
              <Typography variant="caption" className="text-gray-400 uppercase tracking-wider block mb-3">
                流动性因子
              </Typography>
              <Box className="space-y-1">
                {liquidityEntries.map(([k, v]) => (
                  <Box key={k} className="flex items-start gap-2">
                    <Typography variant="caption" className="text-gray-500 min-w-[120px]">{k}</Typography>
                    <Typography variant="caption" className="text-gray-300 break-all">
                      {stringifyValue(v)}
                    </Typography>
                  </Box>
                ))}
              </Box>
            </Box>
          )}
        </>
      )}
    </Box>
  );
};

export default Factors;
