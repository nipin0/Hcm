"""Regime Classifier — Five-Level Market Regime Detection.

Implements the v1.3 five-level regime model:

Priority: ① PRE_TREND → ② TREND → ③ TREND_FADE → ④ RANGE → ⑤ NEUTRAL

Determination:
  PRE_TREND: ADX rising 3K + BBW expanding + price breakout 20-bar
  TREND: ADX ≥ 24 and not falling 3K consecutively
  TREND_FADE: ADX ≥ 24 + falling 3K + BBW/MA20 < 1.0
  RANGE: ADX < 22 + BBW ≤ 1.0
  NEUTRAL: fallback

With 3-bar hybrid confirmation and lock mechanism.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


class Regime(str, Enum):
    """Five-level market regime types."""
    PRE_TREND = "PRE_TREND"
    TREND = "TREND"
    TREND_FADE = "TREND_FADE"
    RANGE = "RANGE"
    NEUTRAL = "NEUTRAL"


@dataclass
class RegimeResult:
    """Regime classification result with diagnostic data."""
    regime: Regime = Regime.NEUTRAL
    strength: float = 0.0  # 0.0-1.0 confidence/strength
    previous_regime: Regime = Regime.NEUTRAL
    adx: float = 20.0
    adx_rising_bars: int = 0
    adx_falling_bars: int = 0
    bbw: float = 1.0
    bbw_ma20: float = 1.0
    bbw_expanding: bool = False  # BBW > 1.2 × BBW_MA20
    breakout_20bar: bool = False  # Price broke 20-bar high/low
    breakout_direction: str = ""  # "UP" or "DOWN"
    trend_direction: str = ""     # Plan B: "UP" / "DOWN" / "" (neutral) — regime trend direction for reverse-suppression
    confirm_bars: int = 0
    lock_bars: int = 0
    transition_reason: str = ""
    pct_b: float = 0.5
    vol_factor: float = 1.0  # P1: effective volatility scaling factor applied this bar


@dataclass
class RegimeConfig:
    """Configurable parameters for regime detection."""
    # PRE_TREND
    pretrend_adx_rising_bars: int = 3
    pretrend_breakout_factor: float = 1.005
    pretrend_breakout_lookback: int = 20
    pretrend_threshold_floor: float = 0.42

    # TREND
    regime_adx_trend: float = 24.0
    trend_strong_adx_threshold: float = 28.0

    # TREND_FADE
    fade_adx_falling_bars: int = 3
    fade_bbw_ratio_max: float = 1.0

    # RANGE
    regime_adx_range: float = 22.0
    range_bbw_max: float = 1.0

    # NEUTRAL 模糊区次级确认（2026-08-26）
    # ADX 落入 [regime_adx_range, regime_adx_trend) 模糊区时，用 hurst（均值回归态）
    # + BBW 趋势（扩张/收窄）细分，避免一律兜底 NEUTRAL 造成的边界漂移：
    #   · hurst < 模糊区趋势阈值 且 BBW 收窄（bbw < bbw_ma20）→ RANGE（震荡）
    #   · hurst ≥ 模糊区趋势阈值 且 (BBW 扩张 或 ADX 上行)  → TREND（趋势初期）
    #   · 否则保持 NEUTRAL
    fuzzy_enable: bool = True                     # 模糊区次级确认总开关
    fuzzy_hurst_trend: float = 0.5                # hurst 趋势/均值回归分界
    fuzzy_bbw_shrink_max: float = 1.0             # BBW 收窄判据：bbw < bbw_ma20 × 此值
    fuzzy_use_bbw: bool = True                    # 是否用 BBW 趋势作为次级确认

    # VOLATILITY-ADAPTIVE (P1)
    # Dynamically scales ADX thresholds and confirmation bars by relative
    # band width (bbw / bbw_ma20): high vol -> tighten (anti-chatter);
    # low vol -> loosen (fast response at trend inception).
    vol_adapt_enable: bool = False
    vol_adapt_scale: float = 0.15      # magnitude; dev*scale clipped to +/-0.4
    vol_adapt_band_ref: float = 1.0    # baseline vol_ratio

    # CONFIRMATION
    switch_pretrend_confirm_bars: int = 0  # Instant trigger
    switch_pretrend_lock_bars: int = 0
    switch_trend_confirm_bars: int = 2
    switch_trend_lock_bars: int = 1
    switch_fade_confirm_bars: int = 2
    switch_fade_lock_bars: int = 0
    switch_range_confirm_bars: int = 2
    switch_range_lock_bars: int = 1


class RegimeClassifier:
    """Classifies market regime using the five-level v1.3 model.

    Combines ADX hierarchy, BBW expansion/contraction, price breakout,
    and Bollinger %b to determine the current market state with
    hybrid confirmation and lock mechanics.

    Example:
        classifier = RegimeClassifier()
        result = classifier.classify(
            adx=26.3, adx_values=[...], bbw=1.15, bbw_ma20=1.0,
            close=4100.0, recent_highs=[...], recent_lows=[...],
            previous_regime=Regime.NEUTRAL,
        )
    """

    def __init__(self, config: Optional[RegimeConfig] = None):
        """Initialize RegimeClassifier.

        Args:
            config: RegimeConfig with custom parameters.
        """
        self._cfg = config or RegimeConfig()
        # Effective (volatility-adapted) parameters for the current bar
        self._eff_adx_trend = self._cfg.regime_adx_trend
        self._eff_adx_range = self._cfg.regime_adx_range
        self._eff_confirm_mult = 1.0
        self._state: dict[str, Any] = {
            "confirm_counter": 0,
            "lock_counter": 0,
            "pending_regime": Regime.NEUTRAL,
        }

    # ── Main API ────────────────────────────────

    def classify(
        self,
        adx: float,
        adx_values: list[float],
        bbw: float,
        bbw_ma20: float,
        close: float,
        recent_highs: list[float],
        recent_lows: list[float],
        pct_b: float = 0.5,
        previous_regime: Regime = Regime.NEUTRAL,
        manual_regime_score: Optional[int] = None,
        plus_di: float = 0.0,
        minus_di: float = 0.0,
        ma_alignment: str = "",
        hurst: float = 0.5,
    ) -> RegimeResult:
        """Classify current market regime.

        Detection priority: PRE_TREND → TREND → TREND_FADE → RANGE → NEUTRAL.

        Args:
            adx: Current ADX value.
            adx_values: Recent ADX values (most recent last).
            bbw: Current BBW (Bollinger Band Width).
            bbw_ma20: 20-bar average of BBW.
            close: Current closing price.
            recent_highs: Recent bar highs.
            recent_lows: Recent bar lows.
            pct_b: Current %b (position within Bollinger Bands).
            previous_regime: Previous classified regime.
            manual_regime_score: Manual override score (None = AI autonomous).

        Returns:
            RegimeResult with regime, strength, and diagnostic info.
        """
        result = RegimeResult(
            previous_regime=previous_regime,
            adx=adx,
            bbw=bbw,
            bbw_ma20=bbw_ma20,
            pct_b=pct_b,
        )

        # P1 — Volatility-adaptive effective parameters.
        # vol_ratio > 1 (expanding bands) -> tighten (higher ADX threshold,
        #   more confirm bars) to suppress chatter in volatile trends.
        # vol_ratio < 1 (contracting bands) -> loosen (lower threshold,
        #   fewer confirm bars) for fast response at trend inception.
        if self._cfg.vol_adapt_enable and bbw_ma20 > 0:
            vol_ratio = bbw / bbw_ma20
            dev = vol_ratio - self._cfg.vol_adapt_band_ref
            factor = 1.0 + max(-0.4, min(0.4, dev * self._cfg.vol_adapt_scale))
            self._eff_adx_trend = self._cfg.regime_adx_trend * factor
            self._eff_adx_range = self._cfg.regime_adx_range * factor
            self._eff_confirm_mult = factor
            result.vol_factor = round(factor, 4)
        else:
            self._eff_adx_trend = self._cfg.regime_adx_trend
            self._eff_adx_range = self._cfg.regime_adx_range
            self._eff_confirm_mult = 1.0
            result.vol_factor = 1.0

        # Manual override takes priority
        if manual_regime_score is not None:
            return self._apply_manual_override(result, manual_regime_score)

        # Compute ADX trend
        result.adx_rising_bars = self._count_rising(adx_values)
        result.adx_falling_bars = self._count_falling(adx_values)

        # BBW expansion check
        result.bbw_expanding = bbw > 1.2 * bbw_ma20 if bbw_ma20 > 0 else False

        # Price breakout check (20-bar)
        result.breakout_20bar, result.breakout_direction = self._check_breakout(
            close, recent_highs, recent_lows, self._cfg.pretrend_breakout_lookback
        )

        # ── Plan B (2026-07-16): 综合判定体制方向（供 scoring 反趋势抑制使用）──
        # 在 breakout 之后即可判定（仅需 breakout_direction + DI + MA + 价格位置）。
        result.trend_direction = self._compute_trend_direction(
            result, plus_di, minus_di, ma_alignment,
            recent_highs, recent_lows, close,
        )

        # ① PRE_TREND
        if self._is_pretrend(result):
            return self._transition_to(result, Regime.PRE_TREND, "PRE_TREND conditions met")

        # ② TREND
        if self._is_trend(result):
            return self._transition_to(result, Regime.TREND, "TREND conditions met")

        # ③ TREND_FADE
        if self._is_trend_fade(result):
            return self._transition_to(result, Regime.TREND_FADE, "TREND_FADE conditions met")

        # ④ RANGE
        if self._is_range(result):
            return self._transition_to(result, Regime.RANGE, "RANGE conditions met")

        # ④.5 NEUTRAL 模糊区次级确认（2026-08-26）
        # ADX ∈ [regime_adx_range, regime_adx_trend) 时，不属于 RANGE 也不属于 TREND，
        # 原逻辑一律兜底 NEUTRAL（边界漂移、误判）。现用 hurst（均值回归态）+ BBW 趋势
        # 细分模糊区：
        #   · hurst<模糊区趋势阈值 且 BBW 收窄   → RANGE（震荡，均值回归）
        #   · hurst≥模糊区趋势阈值 且 (BBW扩张或ADX上行) → TREND（趋势初期）
        #   · 否则保持 NEUTRAL（无强次级信号，不硬归）
        if self._cfg.fuzzy_enable:
            _in_fuzzy = self._eff_adx_range <= adx < self._eff_adx_trend
            if _in_fuzzy:
                _hurst_t = self._cfg.fuzzy_hurst_trend
                _bbw_shrink = (
                    (bbw < self._cfg.fuzzy_bbw_shrink_max * bbw_ma20)
                    if (self._cfg.fuzzy_use_bbw and bbw_ma20 > 0) else False
                )
                _adx_rising = result.adx_rising_bars >= 1
                if hurst < _hurst_t and _bbw_shrink:
                    # 均值回归 + 波动收窄 → 震荡，归入 RANGE
                    return self._transition_to(
                        result, Regime.RANGE,
                        f"NEUTRAL fuzzy→RANGE (hurst={hurst:.3f}<{_hurst_t} bbw_shrink={_bbw_shrink})")
                if hurst >= _hurst_t and (result.bbw_expanding or _adx_rising):
                    # 趋势持续 + 波动扩张/ADX上行 → 趋势初期，归入 TREND
                    return self._transition_to(
                        result, Regime.TREND,
                        f"NEUTRAL fuzzy→TREND (hurst={hurst:.3f}≥{_hurst_t} "
                        f"bbw_expand={result.bbw_expanding} adx_up={_adx_rising})")

        # ⑤ NEUTRAL (fallback)
        result.regime = Regime.NEUTRAL
        result.strength = 0.0
        result.transition_reason = "No regime conditions met — fallback to NEUTRAL"

        self._state["confirm_counter"] = 0
        self._state["lock_counter"] = 0
        self._state["pending_regime"] = Regime.NEUTRAL

        return result

    # ── Detection Methods ───────────────────────

    def _is_pretrend(self, r: RegimeResult) -> bool:
        """Check PRE_TREND conditions.

        ADX rising for 3K + BBW expanding + price breakout 20-bar.

        Args:
            r: Current RegimeResult with computed indicators.

        Returns:
            True if PRE_TREND conditions are met.
        """
        return (
            r.adx_rising_bars >= self._cfg.pretrend_adx_rising_bars
            and r.bbw_expanding
            and r.breakout_20bar
        )

    def _is_trend(self, r: RegimeResult) -> bool:
        """Check TREND conditions.

        ADX ≥ 24 and not falling for 3K consecutively.

        Args:
            r: Current RegimeResult.

        Returns:
            True if TREND conditions are met.
        """
        return (
            r.adx >= self._eff_adx_trend
            and r.adx_falling_bars < self._cfg.fade_adx_falling_bars
        )

    def _is_trend_fade(self, r: RegimeResult) -> bool:
        """Check TREND_FADE conditions.

        ADX ≥ 24 + falling 3K + BBW/MA20 < 1.0.

        Args:
            r: Current RegimeResult.

        Returns:
            True if TREND_FADE conditions are met.
        """
        return (
            r.adx >= self._eff_adx_trend
            and r.adx_falling_bars >= self._cfg.fade_adx_falling_bars
            and r.bbw < self._cfg.fade_bbw_ratio_max * r.bbw_ma20
        ) if r.bbw_ma20 > 0 else False

    def _is_range(self, r: RegimeResult) -> bool:
        """Check RANGE conditions.

        ADX < 22 + BBW ≤ 1.0.

        Args:
            r: Current RegimeResult.

        Returns:
            True if RANGE conditions are met.
        """
        return (
            r.adx < self._eff_adx_range
            and r.bbw <= self._cfg.range_bbw_max
        )

    # ── Confirmation & Lock ────────────────────

    def _transition_to(
        self, result: RegimeResult, target: Regime, reason: str
    ) -> RegimeResult:
        """Handle regime transition with confirmation/lock mechanics.

        Args:
            result: Current RegimeResult.
            target: Target regime.
            reason: Transition reason string.

        Returns:
            Updated RegimeResult.
        """
        current = result.previous_regime

        # Same regime — no transition needed
        if current == target:
            result.regime = target
            result.strength = self._compute_strength(result, target)
            result.transition_reason = f"Maintaining {target.value}: {reason}"
            self._state["confirm_counter"] = 0
            self._state["lock_counter"] = 0
            self._state["pending_regime"] = target
            return result

        # Check lock
        if self._state["lock_counter"] > 0:
            self._state["lock_counter"] -= 1
            result.regime = current
            result.strength = self._compute_strength(result, current)
            result.transition_reason = f"Locked in {current.value} ({self._state['lock_counter']+1} bars remaining)"
            return result

        # Check confirmation
        if self._state["pending_regime"] != target:
            self._state["pending_regime"] = target
            self._state["confirm_counter"] = 1
            confirm_needed = self._get_confirm_bars(target)

            # Fast-path: TREND entry when ADX clearly above threshold
            # Audit 2026-07-14: 0-bar instant entry for strong trends (eliminates 10-min delay)
            if target == Regime.TREND and (
                result.adx > self._cfg.regime_adx_trend + 4 and result.adx_rising_bars >= 1
            ):
                confirm_needed = 0

            if confirm_needed == 0:
                # Instant transition (PRE_TREND)
                result.regime = target
                result.strength = self._compute_strength(result, target)
                result.confirm_bars = 0
                result.lock_bars = self._get_lock_bars(target)
                self._state["lock_counter"] = result.lock_bars
                self._state["confirm_counter"] = 0
                result.transition_reason = f"Instant transition to {target.value}: {reason}"
                return result

            result.regime = current
            result.strength = self._compute_strength(result, current)
            result.transition_reason = f"Pending {target.value} (confirm 1/{confirm_needed})"
            return result

        # Continue confirmation
        self._state["confirm_counter"] += 1
        confirm_needed = self._get_confirm_bars(target)

        if self._state["confirm_counter"] >= confirm_needed:
            # Confirmed!
            result.regime = target
            result.strength = self._compute_strength(result, target)
            result.confirm_bars = confirm_needed
            result.lock_bars = self._get_lock_bars(target)
            self._state["lock_counter"] = result.lock_bars
            self._state["confirm_counter"] = 0
            result.transition_reason = f"Confirmed transition to {target.value} ({confirm_needed} bars): {reason}"
        else:
            result.regime = current
            result.strength = self._compute_strength(result, current)
            result.transition_reason = (
                f"Confirming {target.value} "
                f"({self._state['confirm_counter']}/{confirm_needed})"
            )

        return result

    def _apply_manual_override(
        self, result: RegimeResult, manual_score: int
    ) -> RegimeResult:
        """Apply manual regime override.

        score > 50 → force TREND, strength = (score-50)/50
        score ≤ 50 → force RANGE, strength = (50-score)/50

        Args:
            result: Base RegimeResult.
            manual_score: Manual regime score 0-100.

        Returns:
            Updated RegimeResult.
        """
        if manual_score > 50:
            result.regime = Regime.TREND
            result.strength = (manual_score - 50) / 50.0
            result.transition_reason = f"Manual override → TREND (score={manual_score}, strength={result.strength:.2f})"
        else:
            result.regime = Regime.RANGE
            result.strength = (50 - manual_score) / 50.0
            result.transition_reason = f"Manual override → RANGE (score={manual_score}, strength={result.strength:.2f})"

        result.confirm_bars = 0
        result.lock_bars = 0
        self._state["confirm_counter"] = 0
        self._state["lock_counter"] = 0
        self._state["pending_regime"] = result.regime

        return result

    # ── Strength Calculation ────────────────────

    def _compute_strength(self, r: RegimeResult, regime: Regime) -> float:
        """Compute regime strength on 0.0-1.0 scale.

        Args:
            r: RegimeResult with indicators.
            regime: Current regime.

        Returns:
            Strength value.
        """
        if regime == Regime.PRE_TREND:
            # Strength from breakout clarity + ADX momentum
            adx_factor = min(1.0, r.adx / 30.0)
            return round(0.5 + 0.5 * adx_factor, 3)

        elif regime == Regime.TREND:
            # Strength from ADX level
            if r.adx >= self._cfg.trend_strong_adx_threshold:
                return 0.8
            elif r.adx >= self._cfg.regime_adx_trend:
                return 0.5 + 0.3 * (r.adx - self._cfg.regime_adx_trend) / (
                    self._cfg.trend_strong_adx_threshold - self._cfg.regime_adx_trend
                )
            return 0.5

        elif regime == Regime.TREND_FADE:
            # Strength from ADX decline rate
            fade_ratio = r.adx_falling_bars / self._cfg.fade_adx_falling_bars
            return round(0.5 + 0.3 * min(fade_ratio, 1.0), 3)

        elif regime == Regime.RANGE:
            # Strength from ADX lowness + BBW contraction
            adx_factor = 1.0 - min(1.0, r.adx / self._cfg.regime_adx_range)
            bbw_factor = 1.0 - min(1.0, r.bbw)
            return round(0.5 * adx_factor + 0.5 * bbw_factor, 3)

        else:  # NEUTRAL
            return 0.0

    # ── Helpers ─────────────────────────────────

    def _count_rising(self, values: list[float]) -> int:
        """Count consecutive rising bars from the end.

        Args:
            values: List of values (most recent last).

        Returns:
            Number of consecutive rising bars.
        """
        if len(values) < 2:
            return 0
        count = 0
        for i in range(len(values) - 1, 0, -1):
            if values[i] > values[i - 1]:
                count += 1
            else:
                break
        return count

    def _count_falling(self, values: list[float]) -> int:
        """Count consecutive falling bars from the end.

        Args:
            values: List of values (most recent last).

        Returns:
            Number of consecutive falling bars.
        """
        if len(values) < 2:
            return 0
        count = 0
        for i in range(len(values) - 1, 0, -1):
            if values[i] < values[i - 1]:
                count += 1
            else:
                break
        return count

    def _check_breakout(
        self,
        close: float,
        highs: list[float],
        lows: list[float],
        lookback: int = 20,
    ) -> Tuple[bool, str]:
        """Check if price has broken out of N-bar range.

        Breakout = close > high_Nbar * factor OR close < low_Nbar / factor.

        Args:
            close: Current close.
            highs: Recent high prices.
            lows: Recent low prices.
            lookback: Number of bars to look back.

        Returns:
            Tuple of (is_breakout, direction).
        """
        if len(highs) < lookback or len(lows) < lookback:
            return False, ""

        lookback_highs = highs[-lookback:]
        lookback_lows = lows[-lookback:]
        high_n = max(lookback_highs)
        low_n = min(lookback_lows)

        factor = self._cfg.pretrend_breakout_factor

        if close > high_n * factor:
            return True, "UP"
        elif close < low_n / factor:
            return True, "DOWN"

        return False, ""

    def _compute_trend_direction(
        self,
        r: RegimeResult,
        plus_di: float = 0.0,
        minus_di: float = 0.0,
        ma_alignment: str = "",
        recent_highs: list[float] | None = None,
        recent_lows: list[float] | None = None,
        close: float | None = None,
    ) -> str:
        """综合判定体制方向（UP / DOWN / ""）。

        多源投票（一致更可信）：
          · DI 方向：plus_di - minus_di 的符号（ADX 原生方向，最权威）
          · MA 对齐：bullish / bearish
          · 突破方向：breakout_direction (UP/DOWN)
          · 价格位置：close 在近 20 根 high/low 的相对位置（上 1/3=UP，下 1/3=DOWN）
        返回 "" 表示方向不明确（中性），scoring 应跳过反趋势抑制。
        """
        votes: list[int] = []  # +1=UP, -1=DOWN

        # 1) DI 方向（ADX 原生方向分量）
        if plus_di > 0 and minus_di > 0:
            _di = plus_di - minus_di
            if _di > 1.0:
                votes.append(1)
            elif _di < -1.0:
                votes.append(-1)

        # 2) MA 对齐
        if ma_alignment == "bullish":
            votes.append(1)
        elif ma_alignment == "bearish":
            votes.append(-1)

        # 3) 突破方向
        if r.breakout_direction == "UP":
            votes.append(1)
        elif r.breakout_direction == "DOWN":
            votes.append(-1)

        # 4) 价格位置（近 20 根）
        if recent_highs and recent_lows and close is not None:
            hi = max(recent_highs[-20:])
            lo = min(recent_lows[-20:])
            if hi > lo:
                pos = (close - lo) / (hi - lo)
                if pos > 0.6:
                    votes.append(1)
                elif pos < 0.4:
                    votes.append(-1)

        if not votes:
            return ""
        _net = sum(votes)
        if _net > 0:
            return "UP"
        if _net < 0:
            return "DOWN"
        return ""

    def _get_confirm_bars(self, regime: Regime) -> int:
        """Get confirmation bar count for a regime.

        Args:
            regime: Target regime.

        Returns:
            Number of confirmation bars needed.
        """
        mapping = {
            Regime.PRE_TREND: self._cfg.switch_pretrend_confirm_bars,
            Regime.TREND: self._cfg.switch_trend_confirm_bars,
            Regime.TREND_FADE: self._cfg.switch_fade_confirm_bars,
            Regime.RANGE: self._cfg.switch_range_confirm_bars,
            Regime.NEUTRAL: 0,
        }
        base = mapping.get(regime, 2)
        return int(round(base * self._eff_confirm_mult))

    def _get_lock_bars(self, regime: Regime) -> int:
        """Get lock bar count for a regime.

        Args:
            regime: Target regime.

        Returns:
            Number of lock bars.
        """
        mapping = {
            Regime.PRE_TREND: self._cfg.switch_pretrend_lock_bars,
            Regime.TREND: self._cfg.switch_trend_lock_bars,
            Regime.TREND_FADE: self._cfg.switch_fade_lock_bars,
            Regime.RANGE: self._cfg.switch_range_lock_bars,
            Regime.NEUTRAL: 0,
        }
        return mapping.get(regime, 0)

    # ── Config ──────────────────────────────────

    def update_config(self, config: RegimeConfig) -> None:
        """Update configuration at runtime.

        Args:
            config: New RegimeConfig.
        """
        self._cfg = config
        logger.info("RegimeClassifier config updated")

    @property
    def config(self) -> RegimeConfig:
        """Current configuration."""
        return self._cfg

    def reset_state(self) -> None:
        """Reset internal confirmation/lock state."""
        self._state = {
            "confirm_counter": 0,
            "lock_counter": 0,
            "pending_regime": Regime.NEUTRAL,
        }
