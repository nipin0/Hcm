"""Range Bonus — Oscillation Zone Position Scoring.

Computes position-based score bonuses during RANGE regime using
%b_range (position within the range defined by recent highs/lows
and Bollinger Bands).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ── Default parameters (from PRD §6.1.3) ───────

DEFAULT_RANGE_LOOKBACK = 30
DEFAULT_BOUNDARY_LOW = 0.20
DEFAULT_BOUNDARY_HIGH = 0.80
DEFAULT_BONUS_BOUNDARY = 0.12
DEFAULT_BONUS_NEAR = 0.05
DEFAULT_MULTI_CONFIRM_RSI_LOW = 35.0
DEFAULT_MULTI_CONFIRM_RSI_HIGH = 65.0
DEFAULT_MULTI_CONFIRM_THRESHOLD = 0.10  # %b extreme for multi-confirm
DEFAULT_MULTI_CONFIRM_MULTIPLIER = 1.5


@dataclass
class RangePosition:
    """Range position analysis result."""
    pct_b_range: float = 0.5    # Position within range: 0=bottom, 1=top
    range_low: float = 0.0
    range_high: float = 0.0
    range_width: float = 0.0
    position_label: str = "middle"  # bottom/lower_mid/middle/upper_mid/top
    bonus: float = 0.0
    multi_confirm: bool = False
    multi_confirm_factor: float = 1.0
    direction_hint: str = "neutral"  # BUY-biased, SELL-biased, neutral


@dataclass
class RangeBonusConfig:
    """Configurable parameters for range bonus calculation."""
    lookback_bars: int = DEFAULT_RANGE_LOOKBACK
    boundary_low: float = DEFAULT_BOUNDARY_LOW
    boundary_high: float = DEFAULT_BOUNDARY_HIGH
    bonus_boundary: float = DEFAULT_BONUS_BOUNDARY
    bonus_near: float = DEFAULT_BONUS_NEAR
    multi_confirm_rsi_low: float = DEFAULT_MULTI_CONFIRM_RSI_LOW
    multi_confirm_rsi_high: float = DEFAULT_MULTI_CONFIRM_RSI_HIGH
    multi_confirm_threshold: float = DEFAULT_MULTI_CONFIRM_THRESHOLD
    multi_confirm_multiplier: float = DEFAULT_MULTI_CONFIRM_MULTIPLIER


class RangeBonus:
    """Computes range position-based score adjustments.

    Determines where the current price sits within the recent range
    (defined by N-bar highs/lows and Bollinger Bands), and applies
    direction-specific bonuses with multi-confirmation multipliers.

    Example:
        rb = RangeBonus()
        pos = rb.compute_range_position(
            close=4100.0, rsi=35.2,
            recent_highs=[4120.0, ...], recent_lows=[4080.0, ...],
            boll_upper=4125.0, boll_lower=4075.0,
        )
        # pos.bonus = 0.12 * 1.5 = 0.18 (bottom with multi-confirm)
    """

    def __init__(self, config: Optional[RangeBonusConfig] = None):
        """Initialize RangeBonus.

        Args:
            config: RangeBonusConfig with custom parameters.
        """
        self._cfg = config or RangeBonusConfig()

    # ── Main API ────────────────────────────────

    def compute_range_position(
        self,
        close: float,
        rsi: float,
        recent_highs: list[float],
        recent_lows: list[float],
        boll_upper: float = 0.0,
        boll_lower: float = 0.0,
    ) -> RangePosition:
        """Compute current position within the trading range.

        The range is defined as the overlap of:
          1. Recent N-bar high/low (configurable lookback)
          2. Bollinger Band upper/lower

        Args:
            close: Current closing price.
            rsi: Current RSI-14 value.
            recent_highs: Recent bar highs (most recent last).
            recent_lows: Recent bar lows (most recent last).
            boll_upper: Upper Bollinger Band value.
            boll_lower: Lower Bollinger Band value.

        Returns:
            RangePosition with bonus and confirmation details.
        """
        if not recent_highs or not recent_lows:
            return RangePosition()

        # Compute range boundaries from N-bar extremes
        lookback = min(self._cfg.lookback_bars, len(recent_highs), len(recent_lows))
        range_high = max(recent_highs[-lookback:])
        range_low = min(recent_lows[-lookback:])

        # Refine with Bollinger Bands if available (use the narrower range)
        if boll_upper > 0 and boll_lower > 0:
            range_high = min(range_high, boll_upper * 1.005)  # slight margin
            range_low = max(range_low, boll_lower * 0.995)

        # Ensure valid range
        if range_high <= range_low:
            return RangePosition(
                pct_b_range=0.5,
                range_low=range_low,
                range_high=range_high,
                range_width=0.0,
            )

        range_width = range_high - range_low
        pct_b_range = (close - range_low) / range_width

        # Determine position label
        position_label = self._classify_position(pct_b_range)

        # Calculate bonus
        bonus, direction_hint = self._calculate_bonus(pct_b_range, position_label)

        # Multi-confirmation check
        multi_confirm, multi_factor = self._check_multi_confirm(
            pct_b_range, rsi, position_label
        )

        final_bonus = bonus * multi_factor

        return RangePosition(
            pct_b_range=pct_b_range,
            range_low=range_low,
            range_high=range_high,
            range_width=range_width,
            position_label=position_label,
            bonus=final_bonus,
            multi_confirm=multi_confirm,
            multi_confirm_factor=multi_factor,
            direction_hint=direction_hint,
        )

    # ── Internal Methods ────────────────────────

    def _classify_position(self, pct_b_range: float) -> str:
        """Classify position within range.

        Args:
            pct_b_range: Position value 0.0-1.0.

        Returns:
            Position label string.
        """
        if pct_b_range < self._cfg.boundary_low:
            return "bottom"
        elif pct_b_range < 0.40:
            return "lower_mid"
        elif pct_b_range <= 0.60:
            return "middle"
        elif pct_b_range <= self._cfg.boundary_high:
            return "upper_mid"
        else:
            return "top"

    def _calculate_bonus(
        self, pct_b_range: float, position_label: str
    ) -> Tuple[float, str]:
        """Calculate score bonus based on range position.

        Returns:
            Tuple of (bonus_amount, direction_hint).
        """
        if position_label == "bottom":
            return self._cfg.bonus_boundary, "BUY-biased"
        elif position_label == "lower_mid":
            return self._cfg.bonus_near, "BUY-biased"
        elif position_label == "middle":
            return 0.0, "neutral"
        elif position_label == "upper_mid":
            return self._cfg.bonus_near, "SELL-biased"
        elif position_label == "top":
            return self._cfg.bonus_boundary, "SELL-biased"
        else:
            return 0.0, "neutral"

    def _check_multi_confirm(
        self, pct_b_range: float, rsi: float, position_label: str
    ) -> Tuple[bool, float]:
        """Check for multi-confirmation to multiply bonus.

        Multi-confirmation conditions:
          - Bottom: %b_range < threshold AND RSI < rsi_low
          - Top: %b_range > 1-threshold AND RSI > rsi_high

        Args:
            pct_b_range: Current %b_range.
            rsi: Current RSI value.
            position_label: Position label.

        Returns:
            Tuple of (is_confirmed, multiplier).
        """
        threshold = self._cfg.multi_confirm_threshold

        if position_label == "bottom" and pct_b_range < threshold and rsi < self._cfg.multi_confirm_rsi_low:
            return True, self._cfg.multi_confirm_multiplier
        elif position_label == "top" and pct_b_range > (1.0 - threshold) and rsi > self._cfg.multi_confirm_rsi_high:
            return True, self._cfg.multi_confirm_multiplier
        else:
            return False, 1.0

    # ── ADX-based Range Filtering ───────────────

    def compute_adx_range_filter(
        self,
        adx: float,
        range_adx_exemption: float = 15.0,
        range_adx_weak_weight: float = 0.30,
        range_adx_threshold: float = 22.0,
    ) -> Tuple[float, bool]:
        """Compute ADX-based range filtering weight.

        Per PRD §6.1.3:
          - ADX < 15: fully exempt from direction filtering (weight=0%)
          - 15 ≤ ADX < 20: weak filtering (weight=30%)
          - ADX ≥ 20: moderate filtering (weight=50%)

        Args:
            adx: Current ADX value.
            range_adx_exemption: ADX level below which filtering is fully exempt.
            range_adx_weak_weight: Weight for weak ADX zone.
            range_adx_threshold: Upper bound for range ADX classification.

        Returns:
            Tuple of (weight, fully_exempt).
        """
        if adx < range_adx_exemption:
            return 0.0, True  # Fully exempt
        elif adx < 20.0:
            return range_adx_weak_weight, False
        else:
            return 0.50, False

    # ── Config Update ───────────────────────────

    def update_config(self, config: RangeBonusConfig) -> None:
        """Update configuration at runtime.

        Args:
            config: New RangeBonusConfig.
        """
        self._cfg = config
        logger.info("RangeBonus config updated: lookback=%d, bonuses=(%.2f/%.2f)",
                    config.lookback_bars, config.bonus_boundary, config.bonus_near)

    @property
    def config(self) -> RangeBonusConfig:
        """Current configuration."""
        return self._cfg
