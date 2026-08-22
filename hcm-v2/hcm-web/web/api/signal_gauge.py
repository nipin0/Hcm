"""Signal Gauge Engine — 8-factor computation from K-line data.

Computes RSI, MACD, STOCH, BOLL, MA5, MA20, CCI, ATR from the most recent
M5 klines and maps each factor's raw value into long/neutral/short percentages.

Architecture:
  compute_all(symbol, db_pool) -> {factors: [...], summary: {...}}
    ├── query 20 most recent M5 klines
    ├── compute 8 individual factors
    └── aggregate summary scores

Default Weights (normalized to 1.0):
  RSI=0.15, MACD=0.20, STOCH=0.10, BOLL=0.10, MA5=0.10, MA20=0.15, CCI=0.10, ATR=0.10
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Default weights (sum = 1.0) ──────────────────────────────────────────
_DEFAULT_WEIGHTS: dict[str, float] = {
    "MACD": 0.22,
    "MA5": 0.12,
    "MA20": 0.14,
    "CCI": 0.10,
    "ATR": 0.10,
    "ADX": 0.22,
    "AO": 0.10,
}


def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    """Ensure weights sum to exactly 1.0 by dividing by the total."""
    total: float = sum(weights.values())
    if total <= 0:
        # fallback to uniform
        n: int = len(weights)
        return {k: 1.0 / n for k in weights}
    return {k: v / total for k, v in weights.items()}


# ── Utility: exponential moving average ──────────────────────────────────

def _ema(data: list[float], period: int) -> list[float]:
    """Compute Exponential Moving Average over data.

    Args:
        data: List of values (oldest→newest).
        period: EMA period.

    Returns:
        EMA values of same length as data. First (period-1) entries are SMA.
    """
    if len(data) < period:
        return [sum(data) / len(data)] * len(data) if data else []

    multiplier: float = 2.0 / (period + 1)
    result: list[float] = [0.0] * len(data)

    # Seed with SMA
    result[period - 1] = sum(data[:period]) / period
    for i in range(period, len(data)):
        result[i] = (data[i] - result[i - 1]) * multiplier + result[i - 1]

    # Fill earlier entries with SMA
    sma_init: float = sum(data[:period]) / period
    for i in range(period - 1):
        result[i] = sma_init

    return result


def _sma(data: list[float], period: int) -> list[float]:
    """Simple Moving Average."""
    if len(data) < period:
        avg: float = sum(data) / len(data) if data else 0.0
        return [avg] * len(data)

    result: list[float] = [0.0] * len(data)
    window_sum: float = sum(data[:period])
    for i in range(period - 1, len(data)):
        if i >= period:
            window_sum = window_sum - data[i - period] + data[i]
        result[i] = window_sum / period
    # Fill earlier entries with first valid SMA
    for i in range(period - 1):
        result[i] = result[period - 1]
    return result


def _stddev(data: list[float], period: int, sma_vals: list[float]) -> list[float]:
    """Rolling standard deviation."""
    if len(data) < period:
        return [0.0] * len(data)

    result: list[float] = [0.0] * len(data)
    for i in range(period - 1, len(data)):
        window: list[float] = data[i - period + 1 : i + 1]
        mean: float = sma_vals[i]
        variance: float = sum((x - mean) ** 2 for x in window) / period
        result[i] = variance ** 0.5
    for i in range(period - 1):
        result[i] = result[period - 1]
    return result


# ── Score → long/neutral/short mapping ───────────────────────────────────

def _score_to_pct(score: float) -> tuple[int, int, int]:
    """Map score [0,100] to three-segment percentages.

    score → 0:   extreme short  (short_pct=100)
    score → 50:  pure neutral   (neutral_pct=100)
    score → 100: extreme long   (long_pct=100)

    Mapping rules (from Appendix B):
      score >= 67:  long_pct = (score-50)/50*100, neutral = 100-long, short = 0
      33 <= score < 67: neutral = 100, long = 0, short = 0
      score < 33:   neutral = score/33*100, short = 100-neutral, long = 0

    Args:
        score: Integer or float score in [0, 100].

    Returns:
        (long_pct, neutral_pct, short_pct) tuple of ints that sum to 100.
    """
    score = max(0.0, min(100.0, float(score)))

    if score >= 67.0:
        long_pct: int = round((score - 50.0) / 50.0 * 100.0)
        long_pct = max(0, min(100, long_pct))
        neutral_pct: int = 100 - long_pct
        return (long_pct, neutral_pct, 0)
    elif score >= 33.0:
        return (0, 100, 0)
    else:
        neutral_pct = round(score / 33.0 * 100.0)
        neutral_pct = max(0, min(100, neutral_pct))
        short_pct: int = 100 - neutral_pct
        return (0, neutral_pct, short_pct)


def _raw_to_score(
    raw_value: float,
    extreme_low: float,
    extreme_high: float,
    mid: float = 50.0,
) -> float:
    """Linear mapping of raw_value to score [0, 100].

    raw_value at extreme_low  → score 0 (extreme short)
    raw_value at mid          → score 50 (neutral)
    raw_value at extreme_high → score 100 (extreme long)

    Args:
        raw_value: The raw indicator value.
        extreme_low: Value mapping to score 0.
        extreme_high: Value mapping to score 100.
        mid: Value mapping to score 50 (default: midpoint of extreme_low/extreme_high).

    Returns:
        Score in [0, 100].
    """
    if extreme_high == extreme_low:
        return 50.0

    # Scale linearly: map [extreme_low, mid] → [0, 50], [mid, extreme_high] → [50, 100]
    score: float
    if raw_value <= mid:
        if mid == extreme_low:
            score = 0.0
        else:
            score = (raw_value - extreme_low) / (mid - extreme_low) * 50.0
    else:
        if extreme_high == mid:
            score = 100.0
        else:
            score = 50.0 + (raw_value - mid) / (extreme_high - mid) * 50.0

    return max(0.0, min(100.0, score))


# ── 8 Individual Factor Computations ─────────────────────────────────────

def compute_rsi(closes: list[float]) -> dict[str, Any]:
    """RSI(14): >70 overbought→short, <30 oversold→long.

    Score mapping: RSI=0 → score=100 (extreme long), RSI=100 → score=0 (extreme short),
    RSI=50 → score=50 (neutral).
    """
    period: int = 14
    if len(closes) < period + 1:
        return {"key": "RSI", "raw_value": 50.0, "long_pct": 0, "neutral_pct": 100, "short_pct": 0, "weight": _DEFAULT_WEIGHTS["RSI"]}

    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        diff: float = closes[i] - closes[i - 1]
        gains.append(diff if diff > 0 else 0.0)
        losses.append(abs(diff) if diff < 0 else 0.0)

    avg_gain: float = sum(gains[:period]) / period
    avg_loss: float = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        rsi: float = 100.0
    else:
        rs: float = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))

    # RSI: 0→score=100 (long), 50→score=50, 100→score=0 (short)
    score: float = 100.0 - rsi
    long_pct, neutral_pct, short_pct = _score_to_pct(score)

    return {
        "key": "RSI",
        "raw_value": round(rsi, 2),
        "long_pct": long_pct,
        "neutral_pct": neutral_pct,
        "short_pct": short_pct,
        "weight": _DEFAULT_WEIGHTS["RSI"],
    }


def compute_macd(closes: list[float]) -> dict[str, Any]:
    """MACD(12,26,9): histogram>0→long, <0→short.

    Score mapping: histogram at -2 → score 0, 0 → score 50, +2 → score 100.
    Uses absolute histogram value capped at ±2.
    """
    if len(closes) < 26:
        return {"key": "MACD", "raw_value": 0.0, "long_pct": 0, "neutral_pct": 100, "short_pct": 0, "weight": _DEFAULT_WEIGHTS["MACD"]}

    ema12: list[float] = _ema(closes, 12)
    ema26: list[float] = _ema(closes, 26)

    dif: list[float] = [ema12[i] - ema26[i] for i in range(len(closes))]
    dea: list[float] = _ema(dif, 9)
    hist: float = 2.0 * (dif[-1] - dea[-1])  # MACD histogram × 2 (convention)

    # Map hist [-2, 2] → score [0, 100]
    score: float = _raw_to_score(hist, extreme_low=-2.0, extreme_high=2.0, mid=0.0)
    long_pct, neutral_pct, short_pct = _score_to_pct(score)

    return {
        "key": "MACD",
        "raw_value": round(hist, 4),
        "long_pct": long_pct,
        "neutral_pct": neutral_pct,
        "short_pct": short_pct,
        "weight": _DEFAULT_WEIGHTS["MACD"],
    }


def compute_stoch(highs: list[float], lows: list[float], closes: list[float]) -> dict[str, Any]:
    """Stochastic(14,3,3): %K>80 overbought→short, %K<20 oversold→long.

    Score mapping: %K=0 → score=100 (long), %K=50 → score=50, %K=100 → score=0 (short).
    """
    k_period: int = 14
    if len(closes) < k_period:
        return {"key": "STOCH", "raw_value": 50.0, "long_pct": 0, "neutral_pct": 100, "short_pct": 0, "weight": _DEFAULT_WEIGHTS["STOCH"]}

    # Raw %K
    raw_k: list[float] = []
    for i in range(k_period - 1, len(closes)):
        highest: float = max(highs[i - k_period + 1 : i + 1])
        lowest: float = min(lows[i - k_period + 1 : i + 1])
        if highest == lowest:
            raw_k.append(50.0)
        else:
            raw_k.append((closes[i] - lowest) / (highest - lowest) * 100.0)

    # SMA smoothing of %K (3-period)
    smooth_k: list[float] = _sma(raw_k, 3)
    # %D = SMA(%K, 3)
    # d_vals: list[float] = _sma(smooth_k, 3)

    k_val: float = smooth_k[-1] if smooth_k else 50.0

    # %K: 0→score=100, 50→score=50, 100→score=0
    score: float = 100.0 - k_val
    long_pct, neutral_pct, short_pct = _score_to_pct(score)

    return {
        "key": "STOCH",
        "raw_value": round(k_val, 2),
        "long_pct": long_pct,
        "neutral_pct": neutral_pct,
        "short_pct": short_pct,
        "weight": _DEFAULT_WEIGHTS["STOCH"],
    }


def compute_boll(closes: list[float]) -> dict[str, Any]:
    """Bollinger(20,2): price position within bands.

    raw_value = (close - lower) / (upper - lower), range [0, 1].
    Score mapping: position < 0.2 → long (price near lower band), position > 0.8 → short (near upper).
    """
    period: int = 20
    if len(closes) < period:
        return {"key": "BOLL", "raw_value": 0.5, "long_pct": 0, "neutral_pct": 100, "short_pct": 0, "weight": _DEFAULT_WEIGHTS["BOLL"]}

    sma_vals: list[float] = _sma(closes, period)
    std_vals: list[float] = _stddev(closes, period, sma_vals)

    upper: float = sma_vals[-1] + 2.0 * std_vals[-1]
    lower: float = sma_vals[-1] - 2.0 * std_vals[-1]

    if upper == lower:
        position: float = 0.5
    else:
        position = (closes[-1] - lower) / (upper - lower)

    # position: 0→score=100 (long), 0.5→score=50, 1.0→score=0 (short)
    score: float = 100.0 - position * 100.0
    long_pct, neutral_pct, short_pct = _score_to_pct(score)

    return {
        "key": "BOLL",
        "raw_value": round(position, 4),
        "long_pct": long_pct,
        "neutral_pct": neutral_pct,
        "short_pct": short_pct,
        "weight": _DEFAULT_WEIGHTS["BOLL"],
    }


def compute_ma5(closes: list[float]) -> dict[str, Any]:
    """MA5: price > MA5 → long, price < MA5 → short.

    Score mapping: price/MA5 ratio 0.99→score=0 (short), 1.0→score=50, 1.01→score=100 (long).
    """
    period: int = 5
    if len(closes) < period:
        return {"key": "MA5", "raw_value": 0.0, "long_pct": 0, "neutral_pct": 100, "short_pct": 0, "weight": _DEFAULT_WEIGHTS["MA5"]}

    ma5_val: float = sum(closes[-period:]) / period
    ratio: float = closes[-1] / ma5_val if ma5_val else 1.0

    # ratio: 0.99→score=0, 1.0→score=50, 1.01→score=100
    score: float = _raw_to_score(ratio, extreme_low=0.99, extreme_high=1.01, mid=1.0)
    long_pct, neutral_pct, short_pct = _score_to_pct(score)

    return {
        "key": "MA5",
        "raw_value": round(ratio, 4),
        "long_pct": long_pct,
        "neutral_pct": neutral_pct,
        "short_pct": short_pct,
        "weight": _DEFAULT_WEIGHTS["MA5"],
    }


def compute_ma20(closes: list[float]) -> dict[str, Any]:
    """MA20: price > MA20 → long, price < MA20 → short.

    Score mapping: price/MA20 ratio 0.98→score=0 (short), 1.0→score=50, 1.02→score=100 (long).
    """
    period: int = 20
    if len(closes) < period:
        return {"key": "MA20", "raw_value": 0.0, "long_pct": 0, "neutral_pct": 100, "short_pct": 0, "weight": _DEFAULT_WEIGHTS["MA20"]}

    ma20_val: float = sum(closes[-period:]) / period
    ratio: float = closes[-1] / ma20_val if ma20_val else 1.0

    # ratio: 0.98→score=0, 1.0→score=50, 1.02→score=100
    score: float = _raw_to_score(ratio, extreme_low=0.98, extreme_high=1.02, mid=1.0)
    long_pct, neutral_pct, short_pct = _score_to_pct(score)

    return {
        "key": "MA20",
        "raw_value": round(ratio, 4),
        "long_pct": long_pct,
        "neutral_pct": neutral_pct,
        "short_pct": short_pct,
        "weight": _DEFAULT_WEIGHTS["MA20"],
    }


def compute_cci(highs: list[float], lows: list[float], closes: list[float]) -> dict[str, Any]:
    """CCI(20): >100→long, <-100→short.

    CCI = (TP - SMA(TP,20)) / (0.015 * Mean Deviation)
    Score mapping: CCI -200→score=0 (short), 0→score=50, +200→score=100 (long).
    """
    period: int = 20
    if len(closes) < period:
        return {"key": "CCI", "raw_value": 0.0, "long_pct": 0, "neutral_pct": 100, "short_pct": 0, "weight": _DEFAULT_WEIGHTS["CCI"]}

    # Typical Price
    tp: list[float] = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(len(closes))]
    tp_sma: list[float] = _sma(tp, period)

    # Mean Deviation
    md_vals: list[float] = [0.0] * len(tp)
    for i in range(period - 1, len(tp)):
        mean: float = tp_sma[i]
        total: float = sum(abs(tp[j] - mean) for j in range(i - period + 1, i + 1))
        md_vals[i] = total / period
    for i in range(period - 1):
        md_vals[i] = md_vals[period - 1] if md_vals[period - 1] else 0.001

    # CCI
    divisor: float = 0.015 * md_vals[-1]
    if divisor == 0:
        cci_val: float = 0.0
    else:
        cci_val = (tp[-1] - tp_sma[-1]) / divisor

    # CCI: -200→score=0, 0→score=50, +200→score=100
    score: float = _raw_to_score(cci_val, extreme_low=-200.0, extreme_high=200.0, mid=0.0)
    long_pct, neutral_pct, short_pct = _score_to_pct(score)

    return {
        "key": "CCI",
        "raw_value": round(cci_val, 2),
        "long_pct": long_pct,
        "neutral_pct": neutral_pct,
        "short_pct": short_pct,
        "weight": _DEFAULT_WEIGHTS["CCI"],
    }


def compute_atr(highs: list[float], lows: list[float], closes: list[float]) -> dict[str, Any]:
    """ATR(14): volatility filter — does NOT carry a directional signal.

    It is shown as an auxiliary metric only; it does not contribute to
    the long/neutral/short summary.
    """
    period: int = 14
    if len(closes) < period + 1:
        return {
            "key": "ATR",
            "raw_value": 0.0,
            "long_pct": 0,
            "neutral_pct": 0,
            "short_pct": 0,
            "is_auxiliary": True,
            "weight": _DEFAULT_WEIGHTS["ATR"],
        }

    # True Range
    tr: list[float] = []
    for i in range(1, len(closes)):
        h_l: float = highs[i] - lows[i]
        h_pc: float = abs(highs[i] - closes[i - 1])
        l_pc: float = abs(lows[i] - closes[i - 1])
        tr.append(max(h_l, h_pc, l_pc))

    # ATR (EMA of TR)
    atr_ema: list[float] = _ema(tr, period)
    atr_val: float = atr_ema[-1]

    return {
        "key": "ATR",
        "raw_value": round(atr_val, 4),
        "long_pct": 0,
        "neutral_pct": 0,
        "short_pct": 0,
        "is_auxiliary": True,
        "weight": _DEFAULT_WEIGHTS["ATR"],
    }


def compute_adx(highs: list[float], lows: list[float], closes: list[float]) -> dict[str, Any]:
    """Compute ADX (Average Directional Index) — trend strength indicator.

    ADX measures the *strength* of a trend, not its direction. It is shown as
    an auxiliary metric and does not contribute to the long/neutral/short summary.
    """
    period: int = 14
    n: int = len(highs)
    if n < period + 1:
        return {
            "key": "ADX",
            "raw_value": 20.0,
            "long_pct": 0,
            "neutral_pct": 0,
            "short_pct": 0,
            "is_auxiliary": True,
            "weight": _DEFAULT_WEIGHTS["ADX"],
        }

    # Compute True Range
    tr_list: list[float] = []
    for i in range(1, n):
        hl: float = highs[i] - lows[i]
        hc: float = abs(highs[i] - closes[i - 1])
        lc: float = abs(lows[i] - closes[i - 1])
        tr_list.append(max(hl, hc, lc))

    # Compute +DM and -DM
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, n):
        up: float = highs[i] - highs[i - 1]
        down: float = lows[i - 1] - lows[i]
        if up > down and up > 0:
            plus_dm.append(up)
        else:
            plus_dm.append(0.0)
        if down > up and down > 0:
            minus_dm.append(down)
        else:
            minus_dm.append(0.0)

    def _wilders_series(data: list[float], period: int) -> list[float]:
        """Return the FULL Wilder-smoothed series (same length as input)."""
        if not data:
            return []
        series: list[float] = [0.0] * len(data)
        series[period - 1] = sum(data[:period]) / period
        for i in range(period, len(data)):
            series[i] = series[i - 1] + (data[i] - series[i - 1]) / period
        return series

    # Smooth TR, +DM, -DM as full series
    tr_series: list[float] = _wilders_series(tr_list, period)
    plus_dm_series: list[float] = _wilders_series(plus_dm, period)
    minus_dm_series: list[float] = _wilders_series(minus_dm, period)

    # Build DX series
    dx_series: list[float] = []
    for i in range(period - 1, len(tr_list)):
        atr_i: float = tr_series[i] if tr_series[i] > 0 else 1e-9
        plus_di_i: float = (plus_dm_series[i] / atr_i) * 100.0
        minus_di_i: float = (minus_dm_series[i] / atr_i) * 100.0
        di_sum: float = plus_di_i + minus_di_i
        dx_i: float = (abs(plus_di_i - minus_di_i) / di_sum * 100.0) if di_sum > 0 else 0.0
        dx_series.append(dx_i)

    # ADX = Wilder's smoothing of DX over `period`
    if len(dx_series) < period:
        raw_value: float = round(sum(dx_series) / len(dx_series), 2) if dx_series else 20.0
    else:
        adx_seed: float = sum(dx_series[:period]) / period
        adx_val: float = adx_seed
        for v in dx_series[period:]:
            adx_val = adx_val + (v - adx_val) / period
        raw_value = round(adx_val, 2)

    return {
        "key": "ADX",
        "raw_value": raw_value,
        "long_pct": 0,
        "neutral_pct": 0,
        "short_pct": 0,
        "is_auxiliary": True,
        "weight": _DEFAULT_WEIGHTS["ADX"],
    }


def compute_ao(highs: list[float], lows: list[float]) -> dict[str, Any]:
    """AO (Awesome Oscillator) — Bill Williams momentum indicator.

    AO = SMA5(HL2) - SMA34(HL2) where HL2 = (High + Low) / 2.
    AO > 0 → bullish momentum, AO < 0 → bearish.

    Directional score is a z-score of the current AO against its recent
    (last 20-bar) dispersion, so it is properly sensitive to gold's typically
    small AO magnitudes. Clamped to [-1, +1]; mapped to long/neutral/short.

    Returns:
        Dict with key, raw_value, long_pct, neutral_pct, short_pct, weight.
    """
    import statistics

    if len(highs) < 34:
        return {"key": "AO", "raw_value": 0.0, "long_pct": 0, "neutral_pct": 100, "short_pct": 0,
                "weight": _DEFAULT_WEIGHTS["AO"]}

    hl2: list[float] = [(highs[i] + lows[i]) / 2.0 for i in range(len(highs))]
    n: int = len(hl2)

    def _rolling_sma(series: list[float], period: int) -> list[float]:
        out: list[float] = []
        run: float = 0.0
        for i, v in enumerate(series):
            run += v
            if i >= period:
                run -= series[i - period]
            out.append(run / min(i + 1, period))
        return out

    sma5: list[float] = _rolling_sma(hl2, 5)
    sma34: list[float] = _rolling_sma(hl2, 34)
    ao_series: list[float] = [sma5[i] - sma34[i] for i in range(n)]
    raw_value: float = round(ao_series[-1], 2)

    # z-score scaling vs recent dispersion (gold M5 AO is tiny in price units)
    recent: list[float] = ao_series[-20:]
    std: float = statistics.pstdev(recent) if len(recent) > 1 else (abs(raw_value) or 1.0)
    if std < 1e-9:
        std = 1.0
    raw: float = raw_value / (2.0 * std)
    raw = max(-1.0, min(1.0, raw))

    if raw > 0:
        long_pct: int = int(round(raw * 100.0))
        long_pct = max(0, min(100, long_pct))
        short_pct = 0
        neutral_pct = 100 - long_pct
    elif raw < 0:
        short_pct = int(round(-raw * 100.0))
        short_pct = max(0, min(100, short_pct))
        long_pct = 0
        neutral_pct = 100 - short_pct
    else:
        long_pct = 0
        short_pct = 0
        neutral_pct = 100

    return {
        "key": "AO",
        "raw_value": raw_value,
        "long_pct": long_pct,
        "neutral_pct": neutral_pct,
        "short_pct": short_pct,
        "weight": _DEFAULT_WEIGHTS["AO"],
    }




async def _read_live_adx(symbol: str, redis_client: Any) -> Optional[dict[str, Any]]:
    """Read live ADX from Redis (published by signal-tower every 5s).

    Key: hcm:live:adx:{symbol}_M5
    ADX is a trend-strength auxiliary metric; it does not carry a directional signal.
    """
    if redis_client is None:
        return None
    try:
        raw = await redis_client.raw.get(f"hcm:live:adx:{symbol}_M5")
        if raw is None:
            return None
        import json as _json_read
        data = _json_read.loads(raw)
        raw_value = data.get("raw_value", 20.0)
        return {
            "key": "ADX",
            "raw_value": raw_value,
            "long_pct": 0,
            "neutral_pct": 0,
            "short_pct": 0,
            "is_auxiliary": True,
            "weight": _DEFAULT_WEIGHTS["ADX"],
        }
    except Exception:
        return None


async def _read_realtime_adx_full(
    symbol: str, redis_client: Any
) -> Optional[dict[str, float]]:
    """Read the real-time ADX level + DI spread from the live key.

    Key: hcm:live:adx:{symbol}_M5  (published every 5s by signal-tower's
    _live_adx_publisher). This is the SAME source the top "传统指标·ADX"
    panel reads, so syncing the gauge's ADX label to it eliminates the
    "snapshot vs realtime" split that previously showed 12.1 in the bar
    while the panel showed 18.0.

    Returns {raw_value, plus_di, minus_di} or None.
    """
    if redis_client is None or not redis_client.is_initialized:
        return None
    try:
        raw = await redis_client.raw.get(f"hcm:live:adx:{symbol}_M5")
        if not raw:
            return None
        data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        return {
            "raw_value": float(data.get("raw_value", 0.0)),
            "plus_di": float(data.get("plus_di", 0.0)),
            "minus_di": float(data.get("minus_di", 0.0)),
        }
    except Exception as exc:
        logger.debug("Realtime ADX read failed for %s: %s", symbol, exc)
        return None


# ── Aggregation ──────────────────────────────────────────────────────────

# Scoring-engine component keys used by the real signal pipeline.
# Order is preserved in the dashboard so operators see a consistent layout.
# NOTE: stoch_freq (KD差频) was removed from the dashboard on 2026-07-16 per
# user request — the rightmost column is now the AO (Awesome Oscillator)
# display-only indicator instead. stoch_freq is still computed by the scoring
# engine for signal generation; we simply no longer render it as a gauge
# column.
_SCORING_COMPONENT_KEYS: list[str] = [
    "ma_alignment",
    "macd",
    "adx",
    "boll",
    "stoch",
    "rsi",
    "bar_momentum",
    "boll_vol",
]

# Components the dashboard intentionally does NOT render as a gauge column.
_DROP_KEYS: set[str] = {"stoch_freq"}


def _component_scores_to_gauge(
    component_scores: dict[str, Any],
    weights: dict[str, float],
) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Convert scoring-engine buy/sell contributions into gauge percentages.

    Each component's raw directional strength is recovered from
    raw = buy/weight - sell/weight (in [-1, +1]). The bar is then split:
      raw > 0 -> long_pct = raw*100,  neutral = 100 - long
      raw < 0 -> short_pct = -raw*100, neutral = 100 - short
      raw = 0 -> neutral = 100

    The composite summary is the sum of weighted buy/sell scores, exactly
    what the scoring engine used before regime/zone overlays.
    """
    factors: list[dict[str, Any]] = []
    total_buy = 0.0
    total_sell = 0.0

    # Preserve canonical order, falling back to whatever the engine emitted.
    ordered_keys = _SCORING_COMPONENT_KEYS + [
        k for k in component_scores if k not in _SCORING_COMPONENT_KEYS
    ]

    for key in ordered_keys:
        vals = component_scores.get(key)
        if vals is None:
            continue
        try:
            buy = float(vals[0])
            sell = float(vals[1])
        except Exception:
            continue
        # Always count every component toward the composite summary so the
        # dashboard verdict still matches the scoring engine — a dropped column
        # (e.g. stoch_freq) keeps contributing its weight even if not rendered.
        total_buy += buy
        total_sell += sell
        if key in _DROP_KEYS:
            continue
        weight = float(weights.get(key, 0.0))
        if weight > 0 and (buy > 0 or sell > 0):
            raw: float = buy / weight - sell / weight
        else:
            raw = 0.0
        raw = max(-1.0, min(1.0, raw))

        if raw > 0:
            long_pct = int(round(raw * 100.0))
            short_pct = 0
            neutral_pct = 100 - long_pct
        elif raw < 0:
            short_pct = int(round(-raw * 100.0))
            long_pct = 0
            neutral_pct = 100 - short_pct
        else:
            long_pct = 0
            short_pct = 0
            neutral_pct = 100

        factors.append({
            "key": key,
            "raw_value": round(raw, 3),
            "long_pct": long_pct,
            "neutral_pct": neutral_pct,
            "short_pct": short_pct,
            "weight": round(weight, 4),
        })

    long_score = round(total_buy * 100.0, 1)
    short_score = round(total_sell * 100.0, 1)
    neutral_score = max(0.0, round(100.0 - long_score - short_score, 1))
    summary = {
        "long": long_score,
        "neutral": neutral_score,
        "short": short_score,
    }
    return factors, summary


# ── Real raw indicator display (P2b) ────────────────────────────────────
# Maps each gauge factor to the AUTHENTIC raw market reading that drives it,
# so the dashboard shows real values (ADX level, +DI/-DI spread, RSI, MACD
# histogram, Bollinger %b/BBW, Stochastic %K/%D, MA values, bar momentum)
# instead of the abstract normalized score.
_ALIGN_CN = {"bullish": "多头", "bearish": "空头", "neutral": "中性"}


def _raw_display_for(key: str, ind: dict[str, Any]) -> tuple[str, str]:
    """Return (short_label, full_detail) for a factor key from raw indicators.

    short_label goes under the bar (compact); full_detail goes in the tooltip.
    """
    align_cn = _ALIGN_CN.get(ind.get("ma_alignment", "neutral"), "中性")
    bar_ratio = 0.0
    if ind.get("atr_14", 0) > 0 and ind.get("bar_range", 0) > 0:
        bar_ratio = ind["bar_range"] / ind["atr_14"]
    bar_dir = "多" if ind.get("close", 0) > ind.get("bar_open", 0) else ("空" if ind.get("close", 0) < ind.get("bar_open", 0) else "-")

    builders = {
        "ma_alignment": (
            f"MA{align_cn}",
            f"MA10={ind.get('ma_short', 0):.1f} MA30={ind.get('ma_long', 0):.1f} "
            f"排列={align_cn} 差={(ind.get('ma_short', 0) - ind.get('ma_long', 0)) / max(ind.get('ma_long', 1), 1e-9) * 100:+.2f}%",
        ),
        "macd": (
            f"柱{ind.get('macd_histogram', 0):+.2f}",
            f"MACD线={ind.get('macd', 0):.2f} 信号线={ind.get('macd_signal', 0):.2f} "
            f"柱={ind.get('macd_histogram', 0):+.2f}",
        ),
        "adx": (
            f"ADX{ind.get('adx_14', 0):.1f}",
            f"ADX={ind.get('adx_14', 0):.1f} +DI={ind.get('plus_di', 0):.1f} "
            f"-DI={ind.get('minus_di', 0):.1f} DI差={ind.get('di_diff', 0):+.1f}",
        ),
        "boll": (
            f"%b{ind.get('pct_b', 0):.2f}",
            f"上={ind.get('boll_upper', 0):.1f} 中={ind.get('boll_middle', 0):.1f} "
            f"下={ind.get('boll_lower', 0):.1f} %b={ind.get('pct_b', 0):.2f} BBW={ind.get('bbw', 0):.3f}",
        ),
        "stoch": (
            f"K{ind.get('stoch_k', 0):.0f}/D{ind.get('stoch_d', 0):.0f}",
            f"%K={ind.get('stoch_k', 0):.1f} %D={ind.get('stoch_d', 0):.1f}",
        ),
        "rsi": (
            f"RSI{ind.get('rsi_14', 0):.0f}",
            f"RSI={ind.get('rsi_14', 0):.1f}",
        ),
        "bar_momentum": (
            f"Bar{bar_dir}({bar_ratio:.1f})",
            f"振幅={ind.get('bar_range', 0):.1f} ATR={ind.get('atr_14', 0):.1f} "
            f"比={bar_ratio:.2f} 方向={bar_dir}",
        ),
        "boll_vol": (
            f"BBW{ind.get('bbw', 0):.2f}",
            f"BBW={ind.get('bbw', 0):.3f} MA20={ind.get('bbw_ma20', 0):.3f} "
            f"比={ind.get('bbw', 0) / max(ind.get('bbw_ma20', 1), 1e-9):.2f}",
        ),
    }
    return builders.get(key, ("", ""))


def _attach_raw_displays(
    factors: list[dict[str, Any]],
    indicators: Optional[dict[str, Any]],
    realtime_adx: Optional[dict[str, float]] = None,
) -> None:
    """Attach raw_display / raw_detail to each factor from real indicator data.

    The ADX column's *level* is overridden with the real-time value from
    hcm:live:adx (the same source the top 传统指标·ADX panel uses) so the
    gauge no longer shows a stale 5-min M5 snapshot. The DI spread shown in
    the tooltip still comes from the snapshot `indicators` so it stays
    consistent with the bar's ADX-direction split (scoring-engine snapshot).
    """
    if not indicators and realtime_adx is None:
        return
    for f in factors:
        if f.get("key") == "adx" and realtime_adx is not None:
            snap_adx = indicators.get("adx_14", 0) if indicators else 0.0
            rt_adx = realtime_adx.get("raw_value", snap_adx)
            f["raw_display"] = f"ADX{rt_adx:.1f}"
            f["raw_detail"] = (
                f"ADX={rt_adx:.1f} +DI={indicators.get('plus_di', 0):.1f} "
                f"-DI={indicators.get('minus_di', 0):.1f} "
                f"DI差={indicators.get('di_diff', 0):+.1f}"
            )
            continue
        if not indicators:
            continue
        short, full = _raw_display_for(f.get("key", ""), indicators)
        if short:
            f["raw_display"] = short
        if full:
            f["raw_detail"] = full


async def _load_raw_indicators(
    symbol: str, redis_client: Any
) -> Optional[dict[str, Any]]:
    """Read the real raw indicator readings published by signal-tower."""
    if redis_client is None or not redis_client.is_initialized:
        return None
    try:
        raw = await redis_client.raw.get(f"hcm:live:indicators:{symbol}_M5")
        if not raw:
            return None
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception as exc:
        logger.debug("Raw indicators Redis read failed for %s: %s", symbol, exc)
        return None


async def _load_scoring_engine_components(
    symbol: str, redis_client: Any
) -> Optional[tuple[list[dict[str, Any]], dict[str, float]]]:
    """Read the latest scoring-engine component breakdown from Redis.

    Key: hcm:live:component_scores:{symbol}_M5
    Fallback: parse the newest signal:stream entry's component_scores field.
    """
    if redis_client is None or not redis_client.is_initialized:
        return None

    # Real-time ADX from the SAME source the top 传统指标·ADX panel reads,
    # so the gauge's ADX label is synced to it (option A: display sync only).
    realtime_adx = await _read_realtime_adx_full(symbol, redis_client)

    try:
        raw = await redis_client.raw.get(f"hcm:live:component_scores:{symbol}_M5")
        if raw:
            payload = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
            factors, summary = _component_scores_to_gauge(
                payload.get("component_scores", {}),
                payload.get("weights", {}),
            )
            indicators = await _load_raw_indicators(symbol, redis_client)
            _attach_raw_displays(factors, indicators, realtime_adx)
            return factors, summary
    except Exception as exc:
        logger.debug("Component scores Redis key failed for %s: %s", symbol, exc)

    # Fallback: newest signal:stream entry
    try:
        entries = await redis_client.raw.xrevrange("signal:stream", "+", "-", count=1)
        if entries:
            _mid, fields = entries[0]
            data: dict[str, Any] = {}
            for k, v in fields.items():
                key = k.decode() if isinstance(k, bytes) else k
                val = v.decode() if isinstance(v, bytes) else v
                data[key] = val
            cs_raw = data.get("component_scores", "{}")
            if cs_raw:
                cs = json.loads(cs_raw) if isinstance(cs_raw, str) else cs_raw
                # Stream does not carry weights; derive uniform fallback.
                weights = {k: round(1.0 / len(cs), 4) for k in cs} if cs else {}
                factors, summary = _component_scores_to_gauge(cs, weights)
                indicators = await _load_raw_indicators(symbol, redis_client)
                _attach_raw_displays(factors, indicators, realtime_adx)
                return factors, summary
    except Exception as exc:
        logger.debug("Component scores stream fallback failed for %s: %s", symbol, exc)

    return None


async def _compute_ao_factor(
    symbol: str, db_pool: Any
) -> Optional[dict[str, Any]]:
    """Compute the AO (Awesome Oscillator) factor from real M5 klines.

    Replaces the removed stoch_freq (KD差频) column as the rightmost
    gauge bar. Display-only: it is NOT part of the scoring-engine composite
    summary (the summary stays the engine's verdict). AO = SMA5(HL2) −
    SMA34(HL2); positive = bullish momentum, negative = bearish.
    """
    if db_pool is None or not db_pool.is_initialized:
        return None
    try:
        rows = await db_pool.fetch(
            """SELECT high, low
               FROM hcm_market.klines
               WHERE symbol = $1 AND time_frame = 'M5'
               ORDER BY open_time DESC
               LIMIT 100""",
            symbol,
        )
    except Exception as exc:
        logger.debug("AO klines fetch failed for %s: %s", symbol, exc)
        return None
    if not rows or len(rows) < 34:
        return None
    rows_reversed = list(reversed(rows))
    highs = [float(r[0]) for r in rows_reversed]
    lows = [float(r[1]) for r in rows_reversed]
    f = compute_ao(highs, lows)
    f["key"] = "ao"
    f["weight"] = 0.0  # display-only; excluded from composite summary
    f["raw_display"] = f"AO{f['raw_value']:+.2f}"
    f["raw_detail"] = f"AO={f['raw_value']:+.2f} (SMA5(HL2)-SMA34(HL2))"
    return f


async def compute_all(symbol: str, db_pool: Any, redis_client: Any = None) -> dict[str, Any]:
    """Load scoring-engine component scores for a symbol.

    First tries to read the real component breakdown published by
    signal-tower (hcm:live:component_scores:{symbol}_M5). If unavailable,
    falls back to computing an independent set of factors from M5 klines.

    Args:
        symbol: Trading symbol, e.g. "XAUUSD".
        db_pool: DatabasePool instance with fetch method.
        redis_client: Optional RedisClient for live ADX / component scores.

    Returns:
        Dict with keys "factors" (list of factor dicts) and "summary" (long/neutral/short).
    """
    # P2: prefer the real scoring-engine breakdown over our own recomputation
    if redis_client is not None and redis_client.is_initialized:
        loaded = await _load_scoring_engine_components(symbol, redis_client)
        if loaded is not None:
            factors, summary = loaded
            if factors:
                # Replace the removed KD差频 column with the real AO indicator
                # as the rightmost display-only bar (computed from M5 klines).
                ao = await _compute_ao_factor(symbol, db_pool)
                if ao:
                    factors.append(ao)
                return {"factors": factors, "summary": summary}

    if db_pool is None or not db_pool.is_initialized:
        return _empty_response()

    try:
        rows = await db_pool.fetch(
            """SELECT open, high, low, close
               FROM hcm_market.klines
               WHERE symbol = $1 AND time_frame = 'M5'
               ORDER BY open_time DESC
               LIMIT 100""",
            symbol,
        )
    except Exception as exc:
        logger.error("Failed to query klines for %s: %s", symbol, exc)
        return _empty_response()

    if not rows or len(rows) < 14:
        logger.warning("Insufficient klines (%d) for %s, returning empty", len(rows) if rows else 0, symbol)
        return _empty_response()

    # Reverse to chronological order (oldest→newest)
    rows_reversed: list[Any] = list(reversed(rows))
    # Use integer indexing (row[0]=open, row[1]=high, row[2]=low, row[3]=close)
    closes: list[float] = [float(r[3]) for r in rows_reversed]
    highs: list[float] = [float(r[1]) for r in rows_reversed]
    lows: list[float] = [float(r[2]) for r in rows_reversed]

    # ── ADX: single source of truth from signal-tower (Redis) ──
    adx_factor: dict[str, Any] = await _read_live_adx(symbol, redis_client)
    if adx_factor is None:
        # Fallback: compute locally (only if Redis is down)
        adx_factor = compute_adx(highs, lows, closes)

    # Compute 6 factors (MACD, MA5, MA20, CCI, ATR, AO) — ADX served separately
    factors: list[dict[str, Any]] = [
        compute_macd(closes),          # MACD histogram (acceleration)
        compute_ma5(closes),           # MA5 (same as scoring MA alignment)
        compute_ma20(closes),          # MA20 (trend structure)
        compute_cci(highs, lows, closes),  # CCI (sentiment)
        compute_atr(highs, lows, closes),  # ATR (volatility)
        adx_factor,                    # ADX — from signal-tower via Redis
        compute_ao(highs, lows),       # AO (momentum, display-only)
    ]

    # Ensure the ADX bar shows its real-time level (same as the top panel)
    # even on this fallback path where _attach_raw_displays is not called.
    for f in factors:
        if f.get("key") == "adx":
            f["raw_display"] = f"ADX{f['raw_value']:.1f}"
            f["raw_detail"] = f"ADX={f['raw_value']:.1f} (实时)"
            break

    # Normalize weights ONLY across directional (non-auxiliary) factors
    raw_weights: dict[str, float] = {f["key"]: f["weight"] for f in factors if not f.get("is_auxiliary")}
    norm_weights: dict[str, float] = _normalize_weights(raw_weights)
    for f in factors:
        if f.get("is_auxiliary"):
            f["weight"] = _DEFAULT_WEIGHTS[f["key"]]
        else:
            f["weight"] = round(norm_weights[f["key"]], 4)

    # Compute summary: sum(weight_i * long_pct_i / 100) * 100, etc.
    # Auxiliary factors are excluded from the composite long/neutral/short score.
    long_score: float = 0.0
    short_score: float = 0.0
    for f in factors:
        if f.get("is_auxiliary"):
            continue
        w: float = f["weight"]
        long_score += w * f["long_pct"] / 100.0
        short_score += w * f["short_pct"] / 100.0

    long_score *= 100.0
    short_score *= 100.0
    neutral_score: float = 100.0 - long_score - short_score
    if neutral_score < 0:
        neutral_score = 0.0

    summary: dict[str, float] = {
        "long": round(long_score, 1),
        "neutral": round(neutral_score, 1),
        "short": round(short_score, 1),
    }

    return {"factors": factors, "summary": summary}


def _empty_response() -> dict[str, Any]:
    """Return an empty/neutral response when no data is available."""
    factors: list[dict[str, Any]] = []
    for key, weight in _DEFAULT_WEIGHTS.items():
        is_aux: bool = key in ("ATR", "ADX")
        factors.append({
            "key": key,
            "raw_value": 0.0,
            "long_pct": 0,
            "neutral_pct": 0 if is_aux else 100,
            "short_pct": 0,
            "is_auxiliary": is_aux,
            "weight": weight,
        })
    return {
        "factors": factors,
        "summary": {"long": 0.0, "neutral": 100.0, "short": 0.0},
    }
