"""Technical Indicator Calculator.

Computes standard technical indicators from OHLCV data:
- RSI (Relative Strength Index)
- MACD (Moving Average Convergence Divergence)
- ADX (Average Directional Index)
- Bollinger Bands (with %b and BBW)
- Stochastic Oscillator
- Moving Averages (SMA/EMA)
- Directional Indicators (+DI / -DI)

All calculations use numpy for performance and precision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── Default parameters ─────────────────────────

DEFAULT_RSI_PERIOD = 14
DEFAULT_MACD_FAST = 12
DEFAULT_MACD_SLOW = 26
DEFAULT_MACD_SIGNAL = 9
DEFAULT_ADX_PERIOD = 14
DEFAULT_BOLL_PERIOD = 20
DEFAULT_BOLL_STD = 2.0
DEFAULT_STOCH_K = 14
DEFAULT_STOCH_D = 3
DEFAULT_STOCH_SMOOTH = 3
DEFAULT_MA_SHORT = 10
DEFAULT_MA_LONG = 30


@dataclass
class IndicatorResults:
    """Container for all computed indicator values."""
    # RSI
    rsi_14: float = 50.0
    rsi_values: list[float] = field(default_factory=list)

    # MACD
    macd: float = 0.0
    macd_signal: float = 0.0
    macd_histogram: float = 0.0
    macd_histogram_previous: float = 0.0  # for拐头 detection

    # ADX / DI
    adx_14: float = 20.0
    adx_values: list[float] = field(default_factory=list)
    plus_di: float = 25.0
    minus_di: float = 25.0
    di_diff: float = 0.0  # +DI - (-DI)

    # Bollinger Bands
    boll_upper: float = 0.0
    boll_middle: float = 0.0  # SMA20
    boll_lower: float = 0.0
    pct_b: float = 0.5  # %b = (close - lower) / (upper - lower)
    bbw: float = 1.0  # BBW = (upper - lower) / middle
    bbw_ma20: float = 1.0  # 20-bar average of BBW

    # Stochastic
    stoch_k: float = 50.0
    stoch_d: float = 50.0

    # Moving Averages
    ma_short: float = 0.0  # SMA10 / EMA10
    ma_long: float = 0.0  # SMA30 / EMA30
    ma_short_prev: float = 0.0
    ma_long_prev: float = 0.0

    # MA alignment
    ma_alignment: str = "neutral"  # bullish / bearish / neutral

    # Multi-timeframe direction (aggregated)
    m5_direction: str = "neutral"
    m15_direction: str = "neutral"
    h1_direction: str = "neutral"
    h4_direction: str = "neutral"

    # ── H1 多周期融合上下文 (P0: HMTS 状态判定层) ──
    # 由 h1_regime_classifier 计算并写回，供面板观察与归因落库。
    h1_regime: Optional[str] = None          # BULLISH/BEARISH/RANGE/TRANSITION/None
    h1_trend_direction: str = ""             # UP / DOWN / ""
    h1_trend_strength: float = 0.0           # [0,1] H1 趋势强度（ADX 归一化）
    h1_adx: float = 0.0                      # H1 ADX 值

    # Raw closes for further analysis
    close: float = 0.0
    recent_closes: list[float] = field(default_factory=list)
    recent_highs: list[float] = field(default_factory=list)
    recent_lows: list[float] = field(default_factory=list)

    # ── ATR (Average True Range) ──
    atr_14: float = 0.0

    # ── Current bar open price ──
    bar_open: float = 0.0


class IndicatorCalculator:
    """Computes technical indicators from OHLCV data.

    All methods are pure functions that take numpy arrays and return
    computed values. No side effects — suitable for parallel execution.

    Example:
        calc = IndicatorCalculator()
        closes = np.array([4100.0, 4102.5, ...])
        results = calc.compute_all(closes, highs, lows)
    """

    def __init__(
        self,
        rsi_period: int = DEFAULT_RSI_PERIOD,
        macd_fast: int = DEFAULT_MACD_FAST,
        macd_slow: int = DEFAULT_MACD_SLOW,
        macd_signal: int = DEFAULT_MACD_SIGNAL,
        adx_period: int = DEFAULT_ADX_PERIOD,
        boll_period: int = DEFAULT_BOLL_PERIOD,
        boll_std: float = DEFAULT_BOLL_STD,
        stoch_k: int = DEFAULT_STOCH_K,
        stoch_d: int = DEFAULT_STOCH_D,
        stoch_smooth: int = DEFAULT_STOCH_SMOOTH,
        ma_short: int = DEFAULT_MA_SHORT,
        ma_long: int = DEFAULT_MA_LONG,
    ):
        """Initialize IndicatorCalculator with configurable periods.

        Args:
            rsi_period: RSI lookback period.
            macd_fast: MACD fast EMA period.
            macd_slow: MACD slow EMA period.
            macd_signal: MACD signal line period.
            adx_period: ADX lookback period.
            boll_period: Bollinger Bands SMA period.
            boll_std: Bollinger Bands standard deviation multiplier.
            stoch_k: Stochastic %K period.
            stoch_d: Stochastic %D period.
            stoch_smooth: Stochastic smoothing period.
            ma_short: Short MA period.
            ma_long: Long MA period.
        """
        self._rsi_period = rsi_period
        self._macd_fast = macd_fast
        self._macd_slow = macd_slow
        self._macd_signal = macd_signal
        self._adx_period = adx_period
        self._boll_period = boll_period
        self._boll_std = boll_std
        self._stoch_k = stoch_k
        self._stoch_d = stoch_d
        self._stoch_smooth = stoch_smooth
        self._ma_short = ma_short
        self._ma_long = ma_long

    # ── Main API ────────────────────────────────

    def compute_all(
        self,
        closes: np.ndarray,
        highs: Optional[np.ndarray] = None,
        lows: Optional[np.ndarray] = None,
        opens: Optional[np.ndarray] = None,
    ) -> IndicatorResults:
        """Compute all indicators from price arrays.

        Args:
            closes: Array of closing prices (most recent last).
            highs: Array of high prices. If None, derived from closes.
            lows: Array of low prices. If None, derived from closes.
            opens: Array of open prices. If None, bar_open defaults to 0.0.

        Returns:
            IndicatorResults with all computed values.
        """
        if len(closes) < max(self._boll_period, self._adx_period, self._rsi_period) + 5:
            logger.warning("Insufficient data: %d closes (need >= %d)",
                         len(closes), max(self._boll_period, self._adx_period, self._rsi_period) + 5)
            # 【2026-09-08 审计修复 P1】原此处直接返回"默认值"结果，而 adx_14 默认值是
            # **20.0**、plus_di/minus_di 默认 25.0 —— 恰好落在"有趋势"区间（micro_state
            # 趋势门槛 adx>=18）→ 数据不足时被判成趋势并继续出信号（用假指标下单）。
            # 改为显式返回"指标不可用"的中性值（adx=0 → 下游按震荡/无趋势处理，保守侧）。
            return IndicatorResults(
                close=float(closes[-1]) if len(closes) > 0 else 0.0,
                adx_14=0.0,
                plus_di=0.0,
                minus_di=0.0,
                atr_14=0.0,
            )

        if highs is None:
            highs = closes * 1.001  # Approximate
        if lows is None:
            lows = closes * 0.999  # Approximate

        results = IndicatorResults()
        results.close = float(closes[-1])
        results.recent_closes = closes[-50:].tolist() if len(closes) >= 50 else closes.tolist()
        results.recent_highs = highs[-50:].tolist() if len(highs) >= 50 else highs.tolist()
        results.recent_lows = lows[-50:].tolist() if len(lows) >= 50 else lows.tolist()

        # ── ATR(14) ──
        results.atr_14 = self.compute_atr(highs, lows, closes, period=14)

        # ── Bar open ──
        if opens is not None and len(opens) > 0:
            results.bar_open = float(opens[-1])
        else:
            results.bar_open = 0.0

        # RSI
        rsi_arr = self.compute_rsi(closes)
        results.rsi_14 = float(rsi_arr[-1]) if len(rsi_arr) > 0 else 50.0
        results.rsi_values = rsi_arr[-20:].tolist() if len(rsi_arr) >= 20 else rsi_arr.tolist()

        # MACD
        macd_line, signal_line, histogram = self.compute_macd(closes)
        results.macd = float(macd_line[-1]) if len(macd_line) > 0 else 0.0
        results.macd_signal = float(signal_line[-1]) if len(signal_line) > 0 else 0.0
        results.macd_histogram = float(histogram[-1]) if len(histogram) > 0 else 0.0
        results.macd_histogram_previous = float(histogram[-2]) if len(histogram) >= 2 else 0.0

        # ADX / DI
        adx_arr, plus_di_arr, minus_di_arr = self.compute_adx(highs, lows, closes)
        results.adx_14 = float(adx_arr[-1]) if len(adx_arr) > 0 else 20.0
        results.adx_values = adx_arr[-20:].tolist() if len(adx_arr) >= 20 else adx_arr.tolist()
        results.plus_di = float(plus_di_arr[-1]) if len(plus_di_arr) > 0 else 25.0
        results.minus_di = float(minus_di_arr[-1]) if len(minus_di_arr) > 0 else 25.0
        results.di_diff = results.plus_di - results.minus_di

        # Bollinger Bands
        upper, middle, lower = self.compute_bollinger(closes)
        results.boll_upper = float(upper[-1]) if len(upper) > 0 else 0.0
        results.boll_middle = float(middle[-1]) if len(middle) > 0 else 0.0
        results.boll_lower = float(lower[-1]) if len(lower) > 0 else 0.0
        results.pct_b = self.compute_pct_b(closes[-1], upper[-1], lower[-1])
        results.bbw = self.compute_bbw(upper, middle, lower)

        # BBW MA20
        bbw_values = self.compute_bbw_series(upper, middle, lower)
        if len(bbw_values) >= 20:
            results.bbw_ma20 = float(np.mean(bbw_values[-20:]))
        else:
            results.bbw_ma20 = results.bbw

        # Stochastic
        k, d = self.compute_stochastic(highs, lows, closes)
        results.stoch_k = float(k[-1]) if len(k) > 0 else 50.0
        results.stoch_d = float(d[-1]) if len(d) > 0 else 50.0

        # Moving Averages
        results.ma_short = float(np.mean(closes[-self._ma_short:]))
        results.ma_long = float(np.mean(closes[-self._ma_long:]))
        if len(closes) >= self._ma_short + 1:
            results.ma_short_prev = float(np.mean(closes[-(self._ma_short+1):-1]))
        if len(closes) >= self._ma_long + 1:
            results.ma_long_prev = float(np.mean(closes[-(self._ma_long+1):-1]))

        # MA alignment
        results.ma_alignment = self._compute_ma_alignment(results)

        return results

    # ── RSI ─────────────────────────────────────

    def compute_rsi(self, closes: np.ndarray) -> np.ndarray:
        """Compute RSI (Relative Strength Index).

        RSI = 100 - (100 / (1 + RS)), where RS = avg_gain / avg_loss.

        Args:
            closes: Array of closing prices.

        Returns:
            Array of RSI values (same length as input, first N entries NaN or 50).
        """
        period = self._rsi_period
        if len(closes) < period + 1:
            return np.full_like(closes, 50.0)

        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        rsi = np.full(len(closes), np.nan)

        # Initial Wilder SMA
        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])

        if avg_loss == 0:
            rsi[period] = 100.0
        else:
            rs = avg_gain / avg_loss if avg_loss > 0 else float("inf")
            rsi[period] = 100.0 - (100.0 / (1.0 + rs))

        # Wilder smoothing for remaining
        for i in range(period + 1, len(closes)):
            avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
            if avg_loss == 0:
                rsi[i] = 100.0
            else:
                rs = avg_gain / avg_loss
                rsi[i] = 100.0 - (100.0 / (1.0 + rs))

        return rsi

    # ── MACD ────────────────────────────────────

    def compute_macd(self, closes: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Compute MACD (Moving Average Convergence Divergence).

        MACD Line = EMA(fast) - EMA(slow)
        Signal Line = EMA(MACD Line, signal_period)
        Histogram = MACD Line - Signal Line

        Args:
            closes: Array of closing prices.

        Returns:
            Tuple of (macd_line, signal_line, histogram) arrays.
        """
        if len(closes) < self._macd_slow:
            empty = np.zeros_like(closes)
            return empty, empty, empty

        ema_fast = self._compute_ema(closes, self._macd_fast)
        ema_slow = self._compute_ema(closes, self._macd_slow)
        macd_line = ema_fast - ema_slow
        signal_line = self._compute_ema(macd_line, self._macd_signal)
        histogram = macd_line - signal_line

        return macd_line, signal_line, histogram

    # ── ADX ─────────────────────────────────────

    def compute_adx(
        self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Compute ADX (Average Directional Index) with +DI / -DI.

        Args:
            highs: Array of high prices.
            lows: Array of low prices.
            closes: Array of closing prices.

        Returns:
            Tuple of (adx, plus_di, minus_di) arrays.
        """
        period = self._adx_period
        n = len(closes)
        if n < period + 1:
            empty = np.full(n, 20.0)
            return empty, empty.copy(), empty.copy()

        # True Range
        tr = np.zeros(n)
        plus_dm = np.zeros(n)
        minus_dm = np.zeros(n)

        for i in range(1, n):
            tr[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]

            if up_move > down_move and up_move > 0:
                plus_dm[i] = up_move
                minus_dm[i] = 0
            elif down_move > up_move and down_move > 0:
                plus_dm[i] = 0
                minus_dm[i] = down_move
            else:
                plus_dm[i] = 0
                minus_dm[i] = 0

        # Wilder smoothing
        atr = np.full(n, np.nan)
        atr[period] = np.mean(tr[1:period + 1])
        for i in range(period + 1, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

        smoothed_plus_dm = np.full(n, np.nan)
        smoothed_minus_dm = np.full(n, np.nan)
        smoothed_plus_dm[period] = np.mean(plus_dm[1:period + 1])
        smoothed_minus_dm[period] = np.mean(minus_dm[1:period + 1])
        for i in range(period + 1, n):
            smoothed_plus_dm[i] = (smoothed_plus_dm[i - 1] * (period - 1) + plus_dm[i]) / period
            smoothed_minus_dm[i] = (smoothed_minus_dm[i - 1] * (period - 1) + minus_dm[i]) / period

        # +DI / -DI
        plus_di = np.full(n, 25.0)
        minus_di = np.full(n, 25.0)
        for i in range(period, n):
            if atr[i] > 0:
                plus_di[i] = (smoothed_plus_dm[i] / atr[i]) * 100
                minus_di[i] = (smoothed_minus_dm[i] / atr[i]) * 100

        # ADX
        adx = np.full(n, 20.0)
        dx = np.zeros(n)
        for i in range(period, n):
            di_sum = plus_di[i] + minus_di[i]
            if di_sum > 0:
                dx[i] = abs(plus_di[i] - minus_di[i]) / di_sum * 100
        adx[period] = np.mean(dx[period:2 * period]) if n >= 2 * period else np.mean(dx[period:n])
        for i in range(2 * period, n):
            adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period

        return adx, plus_di, minus_di

    # ── Bollinger Bands ─────────────────────────

    def compute_bollinger(self, closes: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Compute Bollinger Bands.

        Middle = SMA(period)
        Upper = Middle + std_mult * std
        Lower = Middle - std_mult * std

        Args:
            closes: Array of closing prices.

        Returns:
            Tuple of (upper, middle, lower) arrays.
        """
        period = self._boll_period
        if len(closes) < period:
            empty = np.full(len(closes), float(closes[-1]) if len(closes) > 0 else 0.0)
            return empty, empty.copy(), empty.copy()

        middle = np.full(len(closes), np.nan)
        upper = np.full(len(closes), np.nan)
        lower = np.full(len(closes), np.nan)

        for i in range(period - 1, len(closes)):
            window = closes[i - period + 1:i + 1]
            middle[i] = np.mean(window)
            std = np.std(window, ddof=1) if len(window) > 1 else 0.0
            upper[i] = middle[i] + self._boll_std * std
            lower[i] = middle[i] - self._boll_std * std

        return upper, middle, lower

    def compute_pct_b(
        self, close: float, upper: float, lower: float
    ) -> float:
        """Compute %b (position within Bollinger Bands).

        %b = (close - lower) / (upper - lower)

        Args:
            close: Current closing price.
            upper: Upper Bollinger Band value.
            lower: Lower Bollinger Band value.

        Returns:
            %b value (may be <0 or >1 for breakouts).
        """
        if upper == lower:
            return 0.5
        return (close - lower) / (upper - lower)

    def compute_bbw(
        self, upper: np.ndarray, middle: np.ndarray, lower: np.ndarray
    ) -> float:
        """Compute BBW (Bollinger Band Width) at the latest bar.

        BBW = (upper - lower) / middle

        Args:
            upper: Upper band array.
            middle: Middle band array.
            lower: Lower band array.

        Returns:
            Latest BBW value.
        """
        idx = -1
        if len(middle) == 0 or middle[idx] == 0 or np.isnan(middle[idx]):
            return 1.0
        return float((upper[idx] - lower[idx]) / middle[idx])

    def compute_bbw_series(
        self, upper: np.ndarray, middle: np.ndarray, lower: np.ndarray
    ) -> np.ndarray:
        """Compute full BBW series.

        Args:
            upper: Upper band array.
            middle: Middle band array.
            lower: Lower band array.

        Returns:
            Array of BBW values.
        """
        mask = (middle != 0) & ~np.isnan(middle)
        bbw = np.zeros_like(middle)
        bbw[mask] = (upper[mask] - lower[mask]) / middle[mask]
        return bbw

    # ── Stochastic ──────────────────────────────

    def compute_stochastic(
        self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """Compute Stochastic Oscillator.

        %K = 100 * (close - lowest_low_N) / (highest_high_N - lowest_low_N)
        %D = SMA(%K, d_period)

        Args:
            highs: Array of high prices.
            lows: Array of low prices.
            closes: Array of closing prices.

        Returns:
            Tuple of (%K, %D) arrays.
        """
        k_period = self._stoch_k
        d_period = self._stoch_d

        n = len(closes)
        if n < k_period:
            return np.full(n, 50.0), np.full(n, 50.0)

        k = np.full(n, np.nan)
        for i in range(k_period - 1, n):
            window_high = np.max(highs[i - k_period + 1:i + 1])
            window_low = np.min(lows[i - k_period + 1:i + 1])
            denom = window_high - window_low
            if denom == 0:
                k[i] = 50.0
            else:
                k[i] = 100.0 * (closes[i] - window_low) / denom

        # %D = SMA of %K
        d = np.full(n, np.nan)
        for i in range(k_period + d_period - 2, n):
            d[i] = np.nanmean(k[i - d_period + 1:i + 1])

        # Fill NaN
        k = np.nan_to_num(k, nan=50.0)
        d = np.nan_to_num(d, nan=50.0)

        return k, d

    # ── Moving Averages ─────────────────────────

    def compute_sma(self, data: np.ndarray, period: int) -> np.ndarray:
        """Compute Simple Moving Average.

        Args:
            data: Input array.
            period: MA period.

        Returns:
            SMA array.
        """
        if len(data) < period:
            return np.full_like(data, np.mean(data))

        sma = np.full(len(data), np.nan)
        for i in range(period - 1, len(data)):
            sma[i] = np.mean(data[i - period + 1:i + 1])
        return sma

    def _compute_ema(self, data: np.ndarray, period: int) -> np.ndarray:
        """Compute Exponential Moving Average (internal).

        Args:
            data: Input array.
            period: EMA period.

        Returns:
            EMA array.
        """
        if len(data) == 0:
            return np.array([])

        multiplier = 2.0 / (period + 1)
        ema = np.full(len(data), np.nan)
        ema[0] = data[0]

        for i in range(1, len(data)):
            if not np.isnan(data[i]):
                ema[i] = (data[i] - ema[i - 1]) * multiplier + ema[i - 1]
            else:
                ema[i] = ema[i - 1]

        return ema

    def _compute_ma_alignment(self, results: IndicatorResults) -> str:
        """Determine MA alignment.

        Bullish: MA_short > MA_long AND MA_short rising
        Bearish: MA_short < MA_long AND MA_short falling

        Returns:
            "bullish", "bearish", or "neutral".
        """
        if results.ma_short == 0 or results.ma_long == 0:
            return "neutral"

        short_rising = results.ma_short > results.ma_short_prev

        if results.ma_short > results.ma_long:
            return "bullish" if short_rising else "neutral"
        elif results.ma_short < results.ma_long:
            return "bearish" if not short_rising else "neutral"
        else:
            return "neutral"

    # ── Utility ─────────────────────────────────

    @staticmethod
    def compute_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
        """Compute ATR (Average True Range).

        Args:
            highs: Array of high prices.
            lows: Array of low prices.
            closes: Array of closing prices.
            period: ATR period.

        Returns:
            Latest ATR value.
        """
        n = len(closes)
        if n < 2:
            return 0.0

        tr = np.zeros(n)
        for i in range(1, n):
            tr[i] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )

        if n <= period:
            return float(np.mean(tr[1:]))

        # Wilder smoothing
        atr = np.mean(tr[1:period + 1])
        for i in range(period + 1, n):
            atr = (atr * (period - 1) + tr[i]) / period

        return float(atr)

    @staticmethod
    def adx_rising(adx_values: list[float], bars: int = 3) -> bool:
        """Check if ADX has been rising for the last N bars.

        Args:
            adx_values: ADX values (most recent last).
            bars: Number of bars to check.

        Returns:
            True if ADX has been strictly rising.
        """
        if len(adx_values) < bars + 1:
            return False

        recent = adx_values[-(bars + 1):]
        # Filter NaN
        recent = [v for v in recent if not np.isnan(v)]
        if len(recent) < bars + 1:
            return False

        return all(recent[i] > recent[i - 1] for i in range(1, len(recent)))

    @staticmethod
    def adx_falling(adx_values: list[float], bars: int = 3) -> bool:
        """Check if ADX has been falling for the last N bars.

        Args:
            adx_values: ADX values (most recent last).
            bars: Number of bars to check.

        Returns:
            True if ADX has been strictly falling.
        """
        if len(adx_values) < bars + 1:
            return False

        recent = adx_values[-(bars + 1):]
        recent = [v for v in recent if not np.isnan(v)]
        if len(recent) < bars + 1:
            return False

        return all(recent[i] < recent[i - 1] for i in range(1, len(recent)))


# ═══════════════════════════════════════════════════════════════════════════
# P0 (2026-07-15): Support/Resistance + Pivot confluence zones for precise entry
# ─────────────────────────────────────────────────────────────────────────────
# Pure functions operating on numpy arrays / scalar OHLC. No DB access, no side
# effects — safe to call from the scheduler's production pipeline.

ZONE_MIN_STRENGTH = 2          # 最小结构层融合数才输出 zone
SR_TIMEFRAME_PRIMARY = "H1"    # 摆动 S/R 母结构主时间框架
SR_TIMEFRAME_FILTER = "H4"     # 高阶过滤（预留）
ATR_CLUSTER_MULT = 0.3         # zone 聚类 / 融合容差 = 0.3 × ATR


@dataclass
class Zone:
    """A price-level zone with its confluence strength."""
    level: float
    ztype: str          # SUPPORT / RESISTANCE / PIVOT / ROUND / SESSION
    strength: int = 0   # number of distinct structure layers converging


def compute_pivots(prev_high: float, prev_low: float, prev_close: float) -> dict:
    """Classic daily pivot points from the previous period's H/L/C.

    Returns {PP, R1..R3, S1..S3}. Structural backbone for XAUUSD.
    """
    pp = (prev_high + prev_low + prev_close) / 3.0
    return {
        "PP": pp,
        "R1": 2.0 * pp - prev_low,
        "S1": 2.0 * pp - prev_high,
        "R2": pp + (prev_high - prev_low),
        "S2": pp - (prev_high - prev_low),
        "R3": prev_high + 2.0 * (pp - prev_low),
        "S3": prev_low - 2.0 * (pp - prev_high),
    }


def _fractal_swings(highs: np.ndarray, lows: np.ndarray, left: int = 2, right: int = 2):
    """Detect fractal swing highs/lows (Bill Williams).

    Returns list of (price, kind) where kind in {'high','low'}.
    """
    swings = []
    n = len(highs)
    for i in range(left, n - right):
        is_high = all(highs[i] > highs[i - k] for k in range(1, left + 1)) and \
                  all(highs[i] > highs[i + k] for k in range(1, right + 1))
        is_low = all(lows[i] < lows[i - k] for k in range(1, left + 1)) and \
                 all(lows[i] < lows[i + k] for k in range(1, right + 1))
        if is_high:
            swings.append((float(highs[i]), "high"))
        elif is_low:
            swings.append((float(lows[i]), "low"))
    return swings


def compute_support_resistance(
    highs: np.ndarray, lows: np.ndarray, atr: float,
    cluster_mult: float = ATR_CLUSTER_MULT, left: int = 2, right: int = 2,
) -> list:
    """Swing S/R via fractal extremes, clustered into zones by ATR proximity.

    Returns list of (level, ztype) with ztype in {SUPPORT, RESISTANCE, SR}.
    """
    if highs is None or lows is None or len(highs) < left + right + 2:
        return []
    swings = _fractal_swings(highs, lows, left, right)
    if not swings:
        return []
    tol = cluster_mult * atr if atr and atr > 0 else 0.0
    ordered = sorted(swings, key=lambda x: x[0])
    clusters: list = []
    cur = [ordered[0]]
    for s in ordered[1:]:
        if tol > 0 and abs(s[0] - cur[-1][0]) <= tol:
            cur.append(s)
        else:
            clusters.append(cur)
            cur = [s]
    clusters.append(cur)
    result = []
    for cl in clusters:
        avg = sum(x[0] for x in cl) / len(cl)
        kinds = {x[1] for x in cl}
        if kinds == {"low"}:
            ztype = "SUPPORT"
        elif kinds == {"high"}:
            ztype = "RESISTANCE"
        else:
            ztype = "SR"
        result.append((avg, ztype))
    return result


def compute_round_levels(price: float, step: float = 50.0, window: int = 2) -> list:
    """Psychological round-number levels near price ($xx50 / $xx00)."""
    if not price:
        return []
    base = int(price // step) * step
    out = []
    for k in range(-window, window + 1):
        lvl = float(base + k * step)
        if abs(lvl - price) <= step * window:
            out.append((lvl, "ROUND"))
    return out


def build_confluence_zones(
    pivots: dict, sr_zones: list, round_levels: list, session_hl: list,
    current_price: float, atr: float = 0.0,
    min_strength: int = ZONE_MIN_STRENGTH, cluster_mult: float = ATR_CLUSTER_MULT,
) -> list:
    """Merge all candidate levels, score confluence, filter by strength.

    A level's strength = number of DISTINCT structure layers whose level falls
    within `cluster_mult × atr` of it. Only zones with strength >= min_strength
    are returned (deduped by proximity, strongest kept).
    """
    # 【2026-09-01 结论：保持 PIVOT 标签不变，改为在下游用「相对位置」判方向】
    # 曾尝试按真实语义拆分(R1-R3→RESISTANCE / S1-S3→SUPPORT)，实测造成严重回归：
    # zone 产出从 ~68% 直接归零（PIVOT/ROUND/SUPPORT/SESSION/RESISTANCE 全部消失），
    # 因 strength 按「标签」去重，拆分后枢轴阻力与分形阻力合并计 1 层 → 普降 1 层，
    # 大量位置卡在 min_strength 之外。
    # 正确解法是：① strength 改按「来源」去重（见下方，与标签解耦）；
    #           ② 方向判定改由下游按「结构位相对现价」决定（hexp_engine 评分段），
    #              不再依赖 ztype 标签，整数关口 ROUND 亦自动生效。
    # 二者叠加后 zone 产出与改造前一致，且方向语义更稳健，故标签维持原状。
    pmap = {
        "PP": "PIVOT", "R1": "PIVOT", "S1": "PIVOT",
        "R2": "PIVOT", "S2": "PIVOT", "R3": "PIVOT", "S3": "PIVOT",
    }
    # 【2026-09-01 修正·按「来源」而非「标签」计共振】
    # 候选统一为 (level, ztype, source)。strength = 汇聚的【不同来源】数量：
    #   PIVOT=枢轴 / SR=分形摆动 / ROUND=整数关口 / SESSION=前日高低
    # 原因：pmap 语义拆分后(R1-R3→RESISTANCE)，枢轴阻力与分形阻力标签相同，若仍按
    # 标签去重则两者合并计 1 层 → strength 普降 1 → 实测 zone 产出几乎归零
    # (PIVOT/ROUND/SUPPORT/SESSION/RESISTANCE 全部消失)。共振的真实含义是
    # 「不同来源指向同一价位」，故应按 source 去重。
    candidates: list = []
    for k, v in (pivots or {}).items():
        if k in pmap:
            candidates.append((float(v), pmap[k], "PIVOT"))
    candidates += [(float(l), str(t), "SR") for (l, t) in (sr_zones or [])]
    candidates += [(float(l), str(t), "ROUND") for (l, t) in (round_levels or [])]
    candidates += [(float(l), str(t), "SESSION") for (l, t) in (session_hl or [])]
    if not candidates:
        return []

    tol = max(cluster_mult * atr, 1.0) if atr and atr > 0 else 1.0
    raw: list = []
    for i, (lvl, typ, src) in enumerate(candidates):
        near = {src}
        for j, (lvl2, typ2, src2) in enumerate(candidates):
            if i == j:
                continue
            if abs(lvl2 - lvl) <= tol:
                near.add(src2)
        strength = len(near)
        if strength >= min_strength:
            raw.append(Zone(level=lvl, ztype=typ, strength=strength))

    # dedupe by tolerance bucket, keep strongest
    merged: dict = {}
    for z in raw:
        key = round(z.level / tol)
        if key not in merged or z.strength > merged[key].strength:
            merged[key] = z
    return list(merged.values())
