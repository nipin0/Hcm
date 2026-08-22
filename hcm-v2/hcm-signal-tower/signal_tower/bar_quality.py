"""Bar (K-line) quality scoring — pure, additive, signal-agnostic.

This module attaches a per-bar *quality* score in [0, 1] to every kline dict.

Design goals (2026-07-30):
  * PURE: only reads OHLC + optional volume/spread/real_volume fields; never
    mutates the signal path.
  * ADDITIVE: callers that ignore the new fields behave exactly as before, so
    enabling quality computation is safe by default (it does NOT change which
    signals are produced). Wiring quality into the gates is a separate task
    (the co_source `f6` filter / scoring multiplier), controlled by config.
  * GRACEFUL: if `spread` / `real_volume` are absent (current PG schema only
    stores `tick_volume`), those dimensions degrade to neutral and the score
    is still computed from OHLC + volume.

Attached fields per bar:
  quality     float[0,1]  overall bar quality (1 = best)
  vol_q       float       tick_volume relative to trailing mean (1.0 = mean)
  spread_q    float       spread relative to trailing mean (1.0 if n/a)
  body_ratio  float       |close-open| / (high-low)
  pin         float       max(wick) / body  (probe / rejection bar indicator)
  outlier     bool        bar range > OUTLIER_MULT * ATR(14)
"""

from __future__ import annotations

import numpy as np

WINDOW = 20          # trailing window for volume/spread normalization
ATR_PERIOD = 14      # ATR period for outlier detection
OUTLIER_MULT = 3.0   # range > OUTLIER_MULT*ATR  => outlier (low quality)
PIN_WICK_MULT = 2.0  # informational: wick > PIN_WICK_MULT*body is a PIN bar


def _rolling_mean_before(values: list[float], i: int, window: int) -> float:
    """Mean of ``values`` in ``[max(0, i-window), i)`` (excludes current bar).

    Falls back to the overall mean when there is no prior data.
    """
    if not values:
        return 0.0
    lo = max(0, i - window)
    seg = values[lo:i]
    if not seg:
        seg = values
    return float(np.mean(seg))


def _atr(highs: list[float], lows: list[float], closes: list[float],
         period: int = ATR_PERIOD) -> float:
    """Simple trailing ATR over true ranges (last ``period`` bars)."""
    n = len(closes)
    if n < 2:
        return 0.0
    trs: list[float] = []
    for i in range(1, n):
        h, l, c_prev = highs[i], lows[i], closes[i - 1]
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        trs.append(tr)
    if len(trs) < period:
        return float(np.mean(trs)) if trs else 0.0
    return float(np.mean(trs[-period:]))


def compute_bar_quality(klines: list[dict]) -> list[dict]:
    """Attach quality fields to each kline dict in-place; return the list.

    Safe on empty input. If ``klines`` already carries quality fields they are
    overwritten (idempotent).
    """
    if not klines:
        return klines

    opens = [float(k.get("open", 0.0) or 0.0) for k in klines]
    highs = [float(k.get("high", 0.0) or 0.0) for k in klines]
    lows = [float(k.get("low", 0.0) or 0.0) for k in klines]
    closes = [float(k.get("close", 0.0) or 0.0) for k in klines]
    vols = [float(k.get("tick_volume") or 0) for k in klines]
    spreads = [float(k.get("spread") or 0) for k in klines]
    has_spread = any(s > 0 for s in spreads)

    atr = _atr(highs, lows, closes)

    for i, k in enumerate(klines):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        rng = (h - l) if (h - l) > 0 else 0.0
        body = abs(c - o)

        body_ratio = body / rng if rng > 0 else 0.0
        upper_wick = max(h - max(o, c), 0.0)
        lower_wick = max(min(o, c) - l, 0.0)
        wick = max(upper_wick, lower_wick)
        pin = (wick / body) if body > 0 else (wick / rng if rng > 0 else 0.0)

        vol = vols[i]
        vol_base = _rolling_mean_before(vols, i, WINDOW)
        vol_q = vol / vol_base if vol_base > 0 else 1.0

        spread_q = 1.0
        if has_spread:
            sp_base = _rolling_mean_before(spreads, i, WINDOW)
            sp = spreads[i]
            # 单根 bar 的 spread<=0（历史数据未落 spread）视为“未知”→ 中性 1.0，
            # 不惩罚也不奖励；只有显著高于均值才降权。
            spread_q = sp / sp_base if (sp_base > 0 and sp > 0) else 1.0

        outlier = (rng > OUTLIER_MULT * atr) if atr > 0 else False

        # ── Composite quality ────────────────────────────────────────────────
        # Start at 1.0; only penalize bad dimensions, never reward above 1.0.
        q = 1.0
        # Low liquidity: below 0.5x mean heavily penalized.
        if vol_q < 0.5:
            q *= min(1.0, 0.5 + vol_q)  # vol_q=0 -> 0.5, 0.5 -> 1.0
        # Wide spread environment penalized.
        if spread_q > 1.5:
            q *= max(0.5, 1.0 - (spread_q - 1.5) * 0.4)
        # Outlier / spike bar strongly penalized (likely data glitch or wick).
        if outlier:
            q *= 0.4
        q = max(0.0, min(1.0, q))

        k["quality"] = round(q, 4)
        k["vol_q"] = round(vol_q, 4)
        k["spread_q"] = round(spread_q, 4)
        k["body_ratio"] = round(body_ratio, 4)
        k["pin"] = round(pin, 4)
        k["outlier"] = bool(outlier)

    return klines
