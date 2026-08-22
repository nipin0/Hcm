import { useState, useEffect, useRef, useCallback } from 'react';
import client from './client';
import type { FactorGauge, SignalGaugeSummary, SignalGaugeResponse, RealtimeQuote, LatestInference, ScoringThresholds } from '../types/signalGauge';
import { DEFAULT_FACTORS, DEFAULT_SUMMARY } from '../types/signalGauge';

const POLL_INTERVAL_MS: number = 3000;

/** Custom hook: fetch signal gauges with 3s polling */
export function useSignalGauges(symbol: string): {
  factors: FactorGauge[];
  summary: SignalGaugeSummary;
  latestInference: LatestInference | null;
  scoringThresholds: ScoringThresholds | null;
  loading: boolean;
  error: string | null;
} {
  const [factors, setFactors] = useState<FactorGauge[]>(DEFAULT_FACTORS);
  const [summary, setSummary] = useState<SignalGaugeSummary>(DEFAULT_SUMMARY);
  const [latestInference, setLatestInference] = useState<LatestInference | null>(null);
  const [scoringThresholds, setScoringThresholds] = useState<ScoringThresholds | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  // Keep last successful data to prevent flicker on error
  const lastFactorsRef = useRef<FactorGauge[]>(DEFAULT_FACTORS);
  const lastSummaryRef = useRef<SignalGaugeSummary>(DEFAULT_SUMMARY);

  const fetchGauges = useCallback(async (): Promise<void> => {
    try {
      const { data } = await client.get<SignalGaugeResponse>(
        `/api/dashboard/signal-gauges?symbol=${encodeURIComponent(symbol)}`,
      );
      if (data.code === 0 && data.data) {
        const newFactors: FactorGauge[] = data.data.factors || DEFAULT_FACTORS;
        const newSummary: SignalGaugeSummary = data.data.summary || DEFAULT_SUMMARY;
        lastFactorsRef.current = newFactors;
        lastSummaryRef.current = newSummary;
        setFactors(newFactors);
        setSummary(newSummary);
        if (data.data.latest_inference) setLatestInference(data.data.latest_inference);
        if (data.data.scoring_thresholds) setScoringThresholds(data.data.scoring_thresholds);
        setError(null);
      } else {
        setError(data.message || 'Unknown error');
      }
    } catch (_err) {
      // Keep last data; show stale indicator
      setError('数据更新失败');
      // Retain previous successful data
      setFactors(lastFactorsRef.current);
      setSummary(lastSummaryRef.current);
    } finally {
      setLoading(false);
    }
  }, [symbol]);

  useEffect(() => {
    // Immediate fetch on mount or symbol change
    setLoading(true);
    fetchGauges();

    const timerId: ReturnType<typeof setInterval> = setInterval(fetchGauges, POLL_INTERVAL_MS);

    return () => {
      clearInterval(timerId);
    };
  }, [fetchGauges]);

  return { factors, summary, latestInference, scoringThresholds, loading, error };
}

/** Custom hook: fetch realtime quote with 3s polling */
export function useRealtimeQuote(symbol: string): {
  quote: RealtimeQuote | null;
  loading: boolean;
  error: string | null;
} {
  const [quote, setQuote] = useState<RealtimeQuote | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<string | null>(null);

  const lastQuoteRef = useRef<RealtimeQuote | null>(null);

  const fetchQuote = useCallback(async (): Promise<void> => {
    try {
      const { data } = await client.get(
        `/api/dashboard/realtime?symbol=${encodeURIComponent(symbol)}&timeframe=M5`,
      );
      if (data.code === 0 && data.data?.latest_kline) {
        const kline = data.data.latest_kline;
        const live = data.data.live_price;  // MT5 real-time tick (bridge every 2s)
        // Derive RealtimeQuote from realtime API response
        const change: number = (kline.close ?? 0) - (kline.open ?? 0);
        const change_pct: number = kline.open ? (change / kline.open) * 100 : 0;
        // bid/ask from MT5 live tick, kline fields for historical OHLCV
        const lastPri = live?.mid ?? kline.close ?? 0;
        const newQuote: RealtimeQuote = {
          symbol,
          last: lastPri,
          change: Number(change.toFixed(2)),
          change_pct: Number(change_pct.toFixed(2)),
          bid: live?.bid ?? kline.low ?? 0,
          ask: live?.ask ?? kline.high ?? 0,
          high: kline.high ?? 0,
          low: kline.low ?? 0,
          open: kline.open ?? 0,
          volume: kline.tick_volume ?? 0,
          updated_at: live?.updated_at ?? kline.open_time ?? '',  // UTC timestamp from MT5 tick
        };
        lastQuoteRef.current = newQuote;
        setQuote(newQuote);
        setError(null);
      } else {
        setError(data.message || 'No data');
      }
    } catch (_err) {
      setError('报价更新失败');
      if (lastQuoteRef.current) {
        setQuote(lastQuoteRef.current);
      }
    } finally {
      setLoading(false);
    }
  }, [symbol]);

  useEffect(() => {
    setLoading(true);
    fetchQuote();

    const timerId: ReturnType<typeof setInterval> = setInterval(fetchQuote, POLL_INTERVAL_MS);

    return () => {
      clearInterval(timerId);
    };
  }, [fetchQuote]);

  return { quote, loading, error };
}
