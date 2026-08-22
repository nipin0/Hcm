/** Single factor signal gauge data */
export interface FactorGauge {
  /** Factor identifier: RSI | MACD | STOCH | BOLL | MA5 | MA20 | CCI | ATR | ADX */
  key: string;
  /** Bullish percentage 0-100 */
  long_pct: number;
  /** Neutral percentage 0-100 */
  neutral_pct: number;
  /** Bearish percentage 0-100 */
  short_pct: number;
  /** Raw indicator value (derived directional score, [-1, 1]) */
  raw_value: number;
  /** Factor weight 0-1 */
  weight: number;
  /** True for volatility/strength metrics that have no directional signal */
  is_auxiliary?: boolean;
  /** Compact REAL raw reading shown under the bar (e.g. "ADX12.5") */
  raw_display?: string;
  /** Full REAL raw reading shown in tooltip (e.g. "ADX=12.5 +DI=.. -DI=.. 差=-16.3") */
  raw_detail?: string;
}

/** Composite signal gauge summary */
export interface SignalGaugeSummary {
  /** Composite bullish score */
  long: number;
  /** Composite neutral score */
  neutral: number;
  /** Composite bearish score */
  short: number;
}

/** Latest inference data from the most recent signal (now from Redis stream) */
export interface LatestInference {
  score: number;
  symbol: string;
  timeframe: string;
  direction: string;
  regime: string;
  updated_at: string | null;
  // ── 5-model collaboration fields (2026-07-15) ──
  adx_14: number;
  live_adx_14: number;       // realtime ADX (from factor gauges, fixes panel ADX staleness)
  weight_scheme: string;
  pre_score: number;
  confidence: number;
  fallback_reason: string;
  zone_level: number;
  zone_type: string;
  zone_strength: number;
  ai_sl_mult: number;
  ai_tp_mult: number;
  entry_trigger_wait: number;
  signal_mode: string;
  signal_id: string;
}

/** Scoring gate thresholds for all 6 regime types */
export interface ScoringThresholds {
  base: number;
  trend_strong: number;
  trend: number;
  pretrend: number;
  fade: number;
  range: number;
  neutral: number;
  strong_adx_threshold: number;
}

/** Full API response body */
export interface SignalGaugeResponse {
  code: number;
  message: string;
  data: {
    factors: FactorGauge[];
    summary: SignalGaugeSummary;
    latest_inference: LatestInference | null;
    scoring_thresholds: ScoringThresholds | null;
  };
}

/** Real-time quote data (subset of realtime API kline fields) */
export interface RealtimeQuote {
  symbol: string;
  last: number;
  change: number;
  change_pct: number;
  bid: number;
  ask: number;
  high: number;
  low: number;
  open: number;
  volume: number;
  updated_at: string;
}

/** Static default factors for initial/placeholder rendering.
 *  These match the real scoring-engine components published from
 *  signal-tower (hcm:live:component_scores:{symbol}_M5).
 */
export const DEFAULT_FACTORS: FactorGauge[] = [
  { key: 'ma_alignment', long_pct: 0, neutral_pct: 100, short_pct: 0, raw_value: 0, weight: 0.20 },
  { key: 'macd', long_pct: 0, neutral_pct: 100, short_pct: 0, raw_value: 0, weight: 0.20 },
  { key: 'adx', long_pct: 0, neutral_pct: 100, short_pct: 0, raw_value: 0, weight: 0.15 },
  { key: 'boll', long_pct: 0, neutral_pct: 100, short_pct: 0, raw_value: 0, weight: 0.15 },
  { key: 'stoch', long_pct: 0, neutral_pct: 100, short_pct: 0, raw_value: 0, weight: 0.10 },
  { key: 'rsi', long_pct: 0, neutral_pct: 100, short_pct: 0, raw_value: 0, weight: 0.10 },
  { key: 'bar_momentum', long_pct: 0, neutral_pct: 100, short_pct: 0, raw_value: 0, weight: 0.05 },
  { key: 'boll_vol', long_pct: 0, neutral_pct: 100, short_pct: 0, raw_value: 0, weight: 0.03 },
  { key: 'ao', long_pct: 0, neutral_pct: 100, short_pct: 0, raw_value: 0, weight: 0 },
];

export const DEFAULT_SUMMARY: SignalGaugeSummary = { long: 0, neutral: 100, short: 0 };
