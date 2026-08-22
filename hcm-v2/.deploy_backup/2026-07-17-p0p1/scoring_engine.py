"""Scoring Engine — Weighted Indicator Scoring with Regime-Aware Schemes.

Core scoring logic for signal production:
1. Computes pre_score from technical indicators using regime-specific weights
2. Determines signal direction (BUY/SELL/NO_TRADE)
3. Applies threshold gating with regime-adjusted thresholds
4. Implements five-level regime weight schemes:
   - PRE_TREND_WEIGHTS, TREND_WEIGHTS, TREND_FADE_WEIGHTS, RANGE_WEIGHTS, NEUTRAL_WEIGHTS
5. Includes bar_momentum scoring (Fix #2): single-bar velocity scoring via bar_range/ATR
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

import numpy as np

from signal_tower.indicator_calculator import IndicatorResults
from signal_tower.regime_classifier import Regime, RegimeResult
from signal_tower.range_bonus import RangeBonus, RangePosition

logger = logging.getLogger(__name__)

# ── Default Scoring / Cooldown Config ────────
# Sourced from shared.signal_tower_defaults (single source of truth)

from shared.signal_tower_defaults import (
    DEFAULT_SCORE_THRESHOLD,
    DEFAULT_TF_COOLDOWN_MULTIPLIER_MIN, DEFAULT_TF_COOLDOWN_MULTIPLIER_MAX,
    DEFAULT_BAR_SECONDS_M5,
    DEFAULT_MARKET_MOMENTUM_THRESHOLD,
    DEFAULT_MARKET_MOMENTUM_WEIGHT,
)

# ── Weight Schemes (Tier 0: 7 components, optimized 2026-07-15) ──
# Removed: di_diff (merged into adx direction — _score_adx_direction already
#   uses DI spread). ATR was never a directional scorer.
# Added: boll (%b band position), rsi (oscillator with regime-aware thresholds)
# macd: uses histogram direction in TREND, histogram拐头 in RANGE (regime-aware scorer select)
# bar_momentum: reduced from 17-24% to 5-12% (M5 single-bar speed is too noisy for direction)

# TREND (optimized): MA leads, MACD + ADX confirm, BOLL secondary, oscillators minimal
# ── ma 22% > macd 20% > adx 18% > boll 14% > bar_momentum 8% > stoch 7% > rsi 5% (+boll_vol 6%)
TREND_WEIGHTS = {
    "ma_alignment": 0.22,
    "macd":         0.20,
    "adx":          0.18,
    "boll":         0.14,
    "bar_momentum": 0.08,
    "stoch":        0.07,
    "rsi":          0.05,
    "boll_vol":     0.06,
}

# TREND_FADE: trend weakening — oscillators start to matter, trend still dominant but fading
# ── macd 18% > ma 16% > adx 14% > stoch 14% > bar_momentum 12% > boll 12% > rsi 10% (+boll_vol 4%)
TREND_FADE_WEIGHTS = {
    "ma_alignment": 0.16,
    "macd":         0.18,
    "adx":          0.14,
    "boll":         0.12,
    "bar_momentum": 0.12,
    "stoch":        0.14,
    "rsi":          0.10,
    "boll_vol":     0.04,
}

# RANGE (optimized): BOLL king, oscillators dominant, trend indicators minimal
# ── boll 25% > stoch 22% > rsi 18% > bar_momentum 10% > macd 10% > ma 7% > adx 5% (+stoch_freq 3%)
RANGE_WEIGHTS = {
    "ma_alignment": 0.07,
    "macd":         0.10,
    "adx":          0.05,
    "boll":         0.25,
    "bar_momentum": 0.10,
    "stoch":        0.22,
    "rsi":          0.18,
    "stoch_freq":   0.03,
}

# NEUTRAL: balanced blend — slight bias toward oscillators (market has no clear direction)
# ── stoch 18% > rsi 16% > macd 16% > ma 14% > boll 14% > adx 10% > bar_momentum 8% (+boll_vol 4%)
NEUTRAL_WEIGHTS = {
    "ma_alignment": 0.14,
    "macd":         0.16,
    "adx":          0.10,
    "boll":         0.14,
    "bar_momentum": 0.08,
    "stoch":        0.18,
    "rsi":          0.16,
    "boll_vol":     0.04,
}

# PRE_TREND: breakout detection — emphasis on bar_momentum (large bar) + trend confirmers
# ── bar_momentum 22% > macd 18% > ma 16% > adx 14% > boll 12% > stoch 10% > rsi 8%
PRE_TREND_WEIGHTS = {
    "ma_alignment": 0.16,
    "macd":         0.18,
    "adx":          0.14,
    "boll":         0.12,
    "bar_momentum": 0.22,
    "stoch":        0.10,
    "rsi":          0.08,
}


@dataclass
class ScoreResult:
    """Scoring result with direction and breakdown."""
    pre_score: float = 0.0
    direction: str = "NO_TRADE"  # BUY / SELL / NO_TRADE
    buy_score: float = 0.0
    sell_score: float = 0.0
    threshold: float = DEFAULT_SCORE_THRESHOLD
    threshold_passed: bool = False
    regime: Regime = Regime.NEUTRAL
    regime_strength: float = 0.0
    weight_scheme: str = "NEUTRAL_WEIGHTS"
    component_scores: dict[str, Tuple[float, float]] = field(default_factory=dict)
    # component_scores: {component: (buy_contribution, sell_contribution)}
    range_position: Optional[RangePosition] = None
    range_bonus_applied: float = 0.0

    # ── Bar momentum (Fix #2) ──
    bar_momentum_applied: float = 0.0

    # ── P1 (2026-07-15): canonical suppress reason (fixes B3 — magic 9.99) ──
    # When the signal is blocked, this carries the *real* reason, not a
    # constructed "below_threshold(score<threshold)" string. Downstream code
    # (signal_publisher, dashboard) reads this directly. Empty string means
    # signal passed all gates normally.
    fallback_reason: str = ""


class ScoringEngine:
    """Weighted technical indicator scoring engine.

    Computes pre_score from indicator values using regime-specific
    weight schemes, determines direction, and applies threshold gating.

    Includes bar_momentum scoring (Fix #2) for single-bar velocity.

    Example:
        engine = ScoringEngine(config_provider)
        score = engine.compute_pre_score(indicator_results, regime_result)
        if score.threshold_passed:
            print(f"Signal: {score.direction} (pre_score={score.pre_score:.2f})")
    """

    def __init__(
        self,
        config_provider: Any = None,
        range_bonus: Optional[RangeBonus] = None,
    ):
        """Initialize ScoringEngine.

        Args:
            config_provider: ConfigProviderV3 for runtime parameters.
            range_bonus: RangeBonus instance for range position scoring.
        """
        self._config = config_provider
        self._range_bonus = range_bonus or RangeBonus()

        # Default parameters — can be overridden via config_provider
        self._base_threshold: float = DEFAULT_SCORE_THRESHOLD
        # ── P2b (defect 6): single authoritative score gate ──
        # Resolves the double-threshold: engine base(0.10)+regime-offset (e.g.
        # TREND strong 0.10-0.05=0.05) vs scheduler min_score_threshold(0.15).
        # Both now read the SAME config value, so the gate is uniform across
        # all regimes. Regime still changes indicator WEIGHTS, never the gate.
        self._min_score_threshold: float = 0.15
        # ── ADX trading floor (config-driven, NO hardcode) ──
        # Signals with ADX below this are forced NO_TRADE — no directional
        # order is ever placed. Default 22.0 aligns with the regime
        # classifier's RANGE boundary (regime_adx_range=22): ADX<22 means
        # the market has no tradable trend strength, so no direction.
        self._min_adx_for_trade: float = 22.0
        self._pretrend_threshold_floor: float = 0.42
        self._fade_threshold_ceiling: float = 0.78
        self._range_threshold_floor: float = 0.40
        self._neutral_threshold_offset: float = 0.10
        self._trend_strong_adx_threshold: float = 28.0
        self._trend_strong_threshold_offset: float = -0.05
        self._pretrend_threshold_offset: float = -0.08
        self._fade_threshold_offset: float = 0.08
        self._range_threshold_offset: float = -0.10
        self._neutral_threshold_ceiling: float = 0.80
        # ── P1-2: TREND regime 专属有效门槛 ──
        # TREND 实测胜率最差(50%)，抬高其门槛把交易集中在高质量区。
        self._trend_min_score: float = 0.40
        # ── P1-3: 可靠性校准软折扣（默认关闭，防小样本过拟合）──
        self._calib_enabled: bool = False
        self._calib: Optional[dict] = None
        self._calib_min_p: float = 0.50   # 仅当实测 p_win < 此值才折扣
        self._calib_min_n: int = 15       # 且仅当桶样本数 ≥ 此值（稀疏桶忽略）

        # ── Plan B (2026-07-16): 反趋势抑制（根治逆势开仓）──
        # trend_reverse_suppress_factor: 反趋势单的软抑制乘数（默认 0.20）
        # strong_trend_block_reverse: 强趋势(adx≥trend_strong_adx_threshold)完全阻断反向单
        self._trend_reverse_suppress_factor: float = 0.20
        self._strong_trend_block_reverse: bool = True

        # ── T1a: 权重方案配置化（外部可调参，缺失回退硬编码默认）──
        # 默认用模块级硬编码 5 套权重；load_config 会从
        # scoring.weight_schemes_json 覆盖（若配置提供器有值）。
        self._weight_schemes: dict = {
            "TREND_WEIGHTS": dict(TREND_WEIGHTS),
            "TREND_FADE_WEIGHTS": dict(TREND_FADE_WEIGHTS),
            "RANGE_WEIGHTS": dict(RANGE_WEIGHTS),
            "NEUTRAL_WEIGHTS": dict(NEUTRAL_WEIGHTS),
            "PRE_TREND_WEIGHTS": dict(PRE_TREND_WEIGHTS),
        }

        # P1 — Volatility-adaptive cooldown (off by default)
        self._cooldown_vol_enable: bool = False
        self._cooldown_vol_scale: float = 0.0

        # ── Bar momentum config (Fix #2) ──
        self._momentum_threshold: float = DEFAULT_MARKET_MOMENTUM_THRESHOLD

    # ── Main API ────────────────────────────────

    def compute_pre_score(
        self,
        indicators: IndicatorResults,
        regime: RegimeResult,
        zone_level: float = 0.0,
        zone_type: str = "",
        zone_strength: int = 0,
        live_adx: Optional[float] = None,
    ) -> ScoreResult:
        """Compute pre_score and direction from indicators and regime.

        Args:
            indicators: Computed indicator values.
            regime: Regime classification result.
            zone_level: Zone structure price level (Tier 2 — optional).
            zone_type: PIVOT / RESISTANCE / SUPPORT (Tier 2 — optional).
            zone_strength: Zone confidence ms 1-3 (Tier 2 — optional).

        Returns:
            ScoreResult with pre_score, direction, threshold, and breakdown.
        """
        result = ScoreResult(
            regime=regime.regime,
            regime_strength=regime.strength,
        )

        # Select weight scheme based on regime (Tier 1: continuous blending)
        bbw = getattr(indicators, 'bbw', 0.0)
        weight_scheme = self._select_weight_scheme(
            regime.regime, adx=getattr(indicators, 'adx_14', 0.0), bbw=bbw
        )
        result.weight_scheme = f"{regime.regime.value}_WEIGHTS"

        # Compute buy/sell component scores
        buy_score, sell_score, components = self._compute_component_scores(
            indicators, weight_scheme, regime.regime
        )
        result.component_scores = components

        # Capture raw bar_momentum score for diagnostics
        if weight_scheme.get("bar_momentum", 0) > 0:
            result.bar_momentum_applied = self._score_bar_momentum(indicators)

        # Apply range bonus if in RANGE regime
        range_bonus = 0.0
        if regime.regime == Regime.RANGE:
            range_pos = self._range_bonus.compute_range_position(
                close=indicators.close,
                rsi=indicators.rsi_14,
                recent_highs=indicators.recent_highs,
                recent_lows=indicators.recent_lows,
                boll_upper=indicators.boll_upper,
                boll_lower=indicators.boll_lower,
            )
            result.range_position = range_pos
            range_bonus = range_pos.bonus

            # Apply range bonus to direction
            if range_pos.direction_hint == "BUY-biased":
                buy_score += range_bonus
            elif range_pos.direction_hint == "SELL-biased":
                sell_score += range_bonus

        result.buy_score = round(buy_score, 4)
        result.sell_score = round(sell_score, 4)
        result.range_bonus_applied = round(range_bonus, 4)

        # ── ADX trading floor (config-driven, default 22.0) ──
        # ── 2026-07-15 fix: ADX floor moved AFTER Tier 2/3 ──
        # See end of function for dynamic adx_floor that respects
        # zone synergy + confluence boost when computing the effective
        # minimum ADX requirement.
        # Save raw scores for diagnostics before T2/T3 modify them.
        raw_buy_score = buy_score
        raw_sell_score = sell_score

        # Determine direction and pre_score
        # Audit 2026-07-14: when pre_score is high but buy≈sell (signals cancel),
        # trust the slightly-dominant side instead of going NO_TRADE.
        # The "no direction" NO_TRADE only applies when score is too low to matter.
        score_diff = buy_score - sell_score
        if abs(score_diff) < 0.01 and max(buy_score, sell_score) < 0.20:
            result.direction = "NO_TRADE"
            result.pre_score = max(buy_score, sell_score)
        elif score_diff > 0:
            result.direction = "BUY"
            result.pre_score = round(buy_score, 4)
        else:
            result.direction = "SELL"
            result.pre_score = round(sell_score, 4)

        # ── Audit 2026-07-14: RSI overheat suppression ──
        # When RSI ≥ 65 (overbought) and direction=BUY, suppress pre_score.
        # When RSI ≤ 35 (oversold) and direction=SELL, suppress pre_score.
        # This prevents buying at the top of a rally (like signal 1104362).
        rsi = indicators.rsi_14
        # ── P1-1: RSI 过热压制提前（65/35 → 58/42），更早识别末端 ──
        if result.direction == "BUY" and rsi >= 58:
            if rsi >= 68:
                factor = 0.5
            else:
                factor = 0.7
            result.pre_score = round(result.pre_score * factor, 4)
            result.range_bonus_applied = f"rsi_overbought({rsi:.0f})x{factor}"
            result.fallback_reason = f"rsi_overbought({rsi:.0f})x{factor}"
        elif result.direction == "SELL" and rsi <= 42:
            if rsi <= 32:
                factor = 0.5
            else:
                factor = 0.7
            result.pre_score = round(result.pre_score * factor, 4)
            result.range_bonus_applied = f"rsi_oversold({rsi:.0f})x{factor}"
            result.fallback_reason = f"rsi_oversold({rsi:.0f})x{factor}"

        # ── Tier 2: Zone structure synergy ──
        # When zone direction aligns with score direction, add a small bonus
        # proportional to zone_strength and proximity. This creates cross-model
        # synergy between Zone(④) and Scoring(①): if both agree on direction,
        # the composite signal is stronger.
        pre_t2t3 = result.pre_score  # save for dynamic ADX floor (2026-07-15)
        if (zone_strength >= 2 and zone_type and result.direction != "NO_TRADE"
                and indicators.atr_14 > 0):
            zone_dir = "SELL" if zone_type in ("PIVOT", "RESISTANCE") else "BUY"
            if zone_dir == result.direction:
                prox = 1.0 - min(1.0, abs(indicators.close - zone_level) / (indicators.atr_14 * 3))
                zone_bonus = round(0.03 * zone_strength * prox, 4)
                if zone_bonus > 0:
                    result.pre_score = round(result.pre_score + zone_bonus, 4)
                    result.range_bonus_applied = (
                        f"zone_{zone_type}(+{zone_bonus:.4f}@prox={prox:.2f})"
                    )

        # ── Tier 3: Confluence multiplier ──
        # Count how many components agree with the chosen direction.
        # High agreement (>4) → signal is more reliable → multiply up.
        # Low agreement (<3) → signal is "lucky winner" of a split vote → suppress.
        if result.direction != "NO_TRADE" and result.component_scores:
            agree = sum(
                1 for b, s in result.component_scores.values()
                if (b > s and result.direction == "BUY") or (s > b and result.direction == "SELL")
            )
            total = len(result.component_scores)
            if total >= 3:
                if agree >= total - 1:
                    factor = 1.15
                elif agree >= 3:
                    factor = 1.00
                else:
                    factor = 0.60
                if factor != 1.00:
                    result.pre_score = round(result.pre_score * factor, 4)

        # ── P1-1: 滞后指标折扣（直击“高分=低胜率”倒挂根因）──
        # 当信号方向主要由 滞后指标(MA对齐 + MACD柱) 驱动，且 ADX 高（趋势已延伸、
        # 末端风险大）时，对 pre_score 打 0.80 折扣。实测 TREND_WEIGHTS 含 42% 滞后
        # 权重、胜率仅 50%；高分追末端→大回撤(MAE 20点)。
        if result.direction != "NO_TRADE" and result.pre_score > 0 and result.component_scores:
            _idx = 0 if result.direction == "BUY" else 1
            _chosen = sum(max(0.0, c[_idx]) for c in result.component_scores.values())
            _ma_macd = (
                result.component_scores.get("ma_alignment", (0.0, 0.0))[_idx]
                + result.component_scores.get("macd", (0.0, 0.0))[_idx]
            )
            _lag_share = (_ma_macd / _chosen) if _chosen > 0 else 0.0
            if _lag_share > 0.40 and indicators.adx_14 > 28:
                result.pre_score = round(result.pre_score * 0.80, 4)
                result.fallback_reason = (
                    f"lagging_discount(adx={indicators.adx_14:.1f},"
                    f"lag_share={_lag_share:.2f})"
                )

        # ── P1-3: 可靠性校准软折扣（默认关闭，防小样本过拟合）──
        # 仅当 (regime, score_bucket) 桶样本充足(n≥min_n) 且实测胜率明显偏低
        # (p_win<min_p) 时，对 pre_score 乘 0.85。永不硬阻断，避免稀疏桶噪声
        # 误杀正常信号。需先开启 scoring.calibration_enabled 并经留出校验。
        if (self._calib_enabled and self._calib
                and result.direction != "NO_TRADE" and result.pre_score > 0):
            _regime = result.regime.value
            _b = ("low" if result.pre_score < 0.3
                  else "mid" if result.pre_score < 0.5 else "high")
            _cell = self._calib.get(_regime, {}).get(_b)
            if (_cell and _cell.get("n", 0) >= self._calib_min_n
                    and _cell.get("p_win", 1.0) < self._calib_min_p):
                result.pre_score = round(result.pre_score * 0.85, 4)
                result.fallback_reason = (
                    f"calib_discount({_regime},{_b},"
                    f"p={_cell['p_win']:.2f},n={_cell['n']})"
                )

        # ── Plan B (2026-07-16): 反趋势抑制门控 — 根治逆势开仓 ──
        # 当体制已判定为趋势类(TREND/PRE_TREND/TREND_FADE)且方向已明确时，
        # 若评分方向与体制方向相反 → 抑制：
        #   · 强趋势(adx ≥ trend_strong_adx_threshold) 且启用强趋势阻断
        #     → 直接 NO_TRADE（完全阻断反向单，杜绝下跌趋势中开 BUY）
        #   · 否则 → pre_score *= trend_reverse_suppress_factor（默认 0.20，
        #     软抑制；TREND 体制下必然压不过 0.40 门槛，等效阻断且保留原因）
        # trend_direction 由 RegimeClassifier 综合 DI/MA/突破/价格位置投票得出。
        _td = getattr(regime, "trend_direction", "") or ""
        _trend_regimes = (Regime.TREND, Regime.PRE_TREND, Regime.TREND_FADE)
        if (result.direction != "NO_TRADE"
                and regime.regime in _trend_regimes
                and _td in ("UP", "DOWN")):
            _reverse = (_td == "DOWN" and result.direction == "BUY") or (
                _td == "UP" and result.direction == "SELL")
            if _reverse:
                _adx = live_adx if live_adx is not None else indicators.adx_14
                if self._strong_trend_block_reverse and _adx >= self._trend_strong_adx_threshold:
                    result.direction = "NO_TRADE"
                    result.pre_score = round(max(raw_buy_score, raw_sell_score), 4)
                    result.threshold = self._min_score_threshold
                    result.threshold_passed = False
                    result.fallback_reason = (
                        f"reverse_blocked(td={_td},adx={_adx:.1f}"
                        f">={self._trend_strong_adx_threshold:.0f})"
                    )
                    return result
                # 软抑制：强趋势阻断未启用，或 ADX 未达强趋势阈值
                result.pre_score = round(
                    result.pre_score * self._trend_reverse_suppress_factor, 4)
                result.fallback_reason = (
                    f"reverse_suppress(td={_td},"
                    f"factor={self._trend_reverse_suppress_factor:.2f})"
                )

        # ── Dynamic ADX floor (moved after T2/T3 — 2026-07-15 fix) ──
        # The ADX floor is no longer a hard 22.0 for all signals. When
        # Tier 2 (zone synergy) and Tier 3 (confluence multiplier) boost
        # the signal, the effective ADX requirement relaxes proportionally:
        #   t2t3_boost >= 0.05  →  floor - 4  (≥16)   e.g. 22→18
        #   t2t3_boost >= 0.02  →  floor - 2  (≥18)   e.g. 22→20
        #   otherwise            →  22 (no relaxation)
        # This allows zone+confluence consensus to rescue high-quality
        # signals in borderline ADX (18-22), while still blocking at
        # very low ADX (<16) where no amount of agreement helps.
        # ── ADX floor 数据源 ──
        # 默认用 bar-close ADX(indicators.adx_14，与最新已收盘信号同源)。
        # 当 live_override 救援传入 live_adx(实时/成形 bar ADX，与面板
        # live_adx_14 同源)时，floor 判定改用实时 ADX —— 这是修复
        # "面板显示 ADX 已激活但引擎用上次收盘 ADX 卡 floor"不同步的根因。
        adx = live_adx if live_adx is not None else indicators.adx_14
        adx_source = "live" if live_adx is not None else "bar"
        t2t3_boost = result.pre_score - pre_t2t3
        # ── P0-3: 关闭动态 ADX floor 反向放松 ──
        # 原逻辑：Tier2/3 加分越高 → floor 越低 (16-20)，在 ADX 弱区放行；
        #   但实测 TREND(高ADX) 是胜率最差 regime，放宽弱 ADX 区恰是错的方向。
        # 现 effective_adx_min 恒等于 min_adx_for_trade，不再随共识放松。
        # ── T2: RANGE regime 跳过 ADX floor（均值回归本就在低 ADX 获利）──
        # 原硬闸门 effective_adx_min 恒等于 22 对所有 regime 生效，而
        # RANGE ⟺ ADX<22，导致 RANGE_WEIGHTS(布林25%/随机22%/RSI18%) 的
        # 均值回归策略被永久拦死（成死代码）。现改为 regime 感知：
        #   - RANGE      → 跳过 floor（仅保留 score 闸门防裸奔）
        #   - TREND/FADE/PRE → 维持 22 floor 不变
        #   - NEUTRAL    → 维持 floor 阻断（NEUTRAL 不该交易）
        if result.regime == Regime.RANGE:
            effective_adx_min = 0.0
            logger.debug(
                "ADX floor skipped (RANGE regime): adx=%.1f src=%s",
                adx, adx_source,
            )
        else:
            effective_adx_min = self._min_adx_for_trade

        if adx < effective_adx_min:
            # blocked: report raw score for diagnostics
            result.pre_score = round(max(raw_buy_score, raw_sell_score), 4)
            result.direction = "NO_TRADE"
            result.threshold = self._min_score_threshold
            result.threshold_passed = False
            result.range_bonus_applied = f"adx_floor({adx:.1f},{adx_source})"
            result.fallback_reason = (
                f"adx_floor({adx:.1f}<{effective_adx_min},{adx_source}, "
                f"t2t3_boost={t2t3_boost:.4f})"
            )
            return result

        # Compute regime-adjusted threshold
        result.threshold = self._compute_threshold(regime)
        result.threshold_passed = result.pre_score >= result.threshold

        return result

    # ── Weight Scheme Selection (Tier 1: full adaptive blending) ──

    def _select_weight_scheme(
        self, regime: Regime, adx: float = 0.0, bbw: float = 0.0
    ) -> dict[str, float]:
        """Select weight scheme with continuous blending between adjacent regimes.

        Instead of hard-switching at regime boundaries, this blends weights
        smoothly using ADX and BBW as continuous transition factors. This
        eliminates the "threshold anxiety" where ADX=23.9→NEUTRAL suddenly
        flips to ADX=24.1→TREND with radically different weights.

        Blend pairs:
          PRE_TREND → TREND:     adx 22→28 (breakout→trend confirmation)
          TREND ←→ TREND_FADE:   adx 28→20 (trend strengthening/weakening)
          TREND_FADE ←→ NEUTRAL: adx 24→18 (fade→no trend)
          NEUTRAL ←→ RANGE:      bbw 0.7→1.0 (tight→wide bands)
          PRETREND: standalone (breakout has unique weight profile)

        Args:
            regime: Current market regime.
            adx: ADX value for direction-aware blending.
            bbw: Bollinger Band Width for volatility-aware blending.

        Returns:
            Dict mapping component name to weight.
        """
        # T1a: 权重方案取自实例属性（配置驱动，缺失回退硬编码默认）
        W = self._weight_schemes
        def _blend(a: dict, b: dict, t: float) -> dict[str, float]:
            """Blend two weight dicts by t in [0,1]. t=0 → pure a, t=1 → pure b."""
            t = max(0.0, min(1.0, t))
            keys = set(a.keys()) | set(b.keys())
            result = {}
            for k in keys:
                result[k] = round(a.get(k, 0) * (1 - t) + b.get(k, 0) * t, 4)
            total = sum(result.values())
            if total > 0:
                result = {k: round(v / total, 4) for k, v in result.items()}
            return result

        # ── TREND ←→ TREND_FADE (adx 20-28) ──
        if regime in (Regime.TREND, Regime.TREND_FADE):
            # TREND: t=0 (pure TREND), TREND_FADE: t=1 (pure FADE)
            # adx 28→100% TREND, adx 20→100% FADE
            t_fade = max(0.0, min(1.0, (28 - adx) / 8))
            if regime == Regime.TREND_FADE:
                t_fade = max(t_fade, 0.5)  # at least 50% FADE when regime says so
            return _blend(W["TREND_WEIGHTS"], W["TREND_FADE_WEIGHTS"], t_fade)

        # ── PRE_TREND → TREND (adx 22-28) ──
        if regime == Regime.PRE_TREND:
            if adx > 22:
                t = min(1.0, (adx - 22) / 6)  # 22→28 maps to 0→1 (PRE_TREND→TREND)
                return _blend(W["PRE_TREND_WEIGHTS"], W["TREND_WEIGHTS"], t)
            return dict(W["PRE_TREND_WEIGHTS"])

        # ── TREND_FADE ←→ NEUTRAL (adx 18-24) ──
        if regime == Regime.NEUTRAL and adx > 18:
            t = min(1.0, max(0.0, (adx - 18) / 6))  # 18→24 maps to 0→1 (NEUTRAL→FADE)
            return _blend(W["NEUTRAL_WEIGHTS"], W["TREND_FADE_WEIGHTS"], t)

        # ── NEUTRAL ←→ RANGE (bbw 0.7-1.0) ──
        if regime in (Regime.RANGE, Regime.NEUTRAL) and bbw > 0.7:
            t = min(1.0, (bbw - 0.7) / 0.3)  # 0.7→1.0 maps to 0→1 (NEUTRAL→RANGE)
            return _blend(W["NEUTRAL_WEIGHTS"], W["RANGE_WEIGHTS"], t)

        # ── Pure regime (no blending zone) ──
        schemes = {
            Regime.PRE_TREND: W["PRE_TREND_WEIGHTS"],
            Regime.TREND: W["TREND_WEIGHTS"],
            Regime.TREND_FADE: W["TREND_FADE_WEIGHTS"],
            Regime.RANGE: W["RANGE_WEIGHTS"],
            Regime.NEUTRAL: W["NEUTRAL_WEIGHTS"],
        }
        return dict(schemes.get(regime, W["NEUTRAL_WEIGHTS"]))

    # ── Component Scoring (Tier 0: 7 components) ──

    def _compute_component_scores(
        self,
        ind: IndicatorResults,
        weights: dict[str, float],
        regime: Regime,
    ) -> Tuple[float, float, dict[str, Tuple[float, float]]]:
        """Compute buy/sell scores for each component.

        Each component generates a raw score in [-1, 1] where:
          +1 = strongly BUY
          -1 = strongly SELL

        Components are split: buy_score = max(raw, 0) * w, sell_score = max(-raw, 0) * w.

        MACD scorer selection is regime-aware:
          TREND/TREND_FADE → _score_macd (histogram direction, continuous)
          RANGE/NEUTRAL    → _score_macd_histogram (histogram拐头, acceleration)

        Args:
            ind: Indicator results.
            weights: Weight scheme dict.
            regime: Current regime.

        Returns:
            Tuple of (buy_score, sell_score, components_dict).
        """
        components: dict[str, Tuple[float, float]] = {}
        total_buy = 0.0
        total_sell = 0.0

        def _add(key: str, raw: float, w_key: str):
            nonlocal total_buy, total_sell
            w = weights.get(w_key, 0)
            if w <= 0:
                return
            buy = max(0.0, raw) * w
            sell = max(0.0, -raw) * w
            components[key] = (round(buy, 4), round(sell, 4))
            total_buy += buy
            total_sell += sell

        # MACD — histogram direction (trend) or histogram拐头 (range/neutral)
        if weights.get("macd", 0) > 0:
            if regime in (Regime.RANGE, Regime.NEUTRAL):
                raw = self._score_macd_histogram(ind)
            else:
                raw = self._score_macd(ind)
            _add("macd", raw, "macd")

        # MA Alignment
        _add("ma_alignment", self._score_ma_alignment(ind), "ma_alignment")

        # ADX direction (merged: was di_diff + adx — now single scorer)
        _add("adx", self._score_adx_direction(ind), "adx")

        # BOLL (%b band position)
        _add("boll", self._score_pct_b(ind), "boll")

        # Stochastic oscillator
        _add("stoch", self._score_stoch(ind), "stoch")

        # RSI oscillator (standard thresholds for TREND, wider for RANGE)
        if weights.get("rsi", 0) > 0:
            raw = self._score_rsi(ind, use_extreme=(regime == Regime.RANGE))
            _add("rsi", raw, "rsi")

        # Bar Momentum (M5 single-bar velocity, reduced weight)
        _add("bar_momentum", self._score_bar_momentum(ind), "bar_momentum")

        # Boll Volatility (Tier 0: BBW expansion/contraction)
        _add("boll_vol", self._score_boll_vol(ind), "boll_vol")

        # Stochastic Frequency (Tier 0: %K-%D spread as oscillation proxy)
        _add("stoch_freq", self._score_stoch_freq(ind), "stoch_freq")

        return round(total_buy, 4), round(total_sell, 4), components

    # ── Individual Component Scorers ────────────

    @staticmethod
    def _score_macd(ind: IndicatorResults) -> float:
        """Score MACD: histogram positive = BUY, negative = SELL.

        Returns:
            Raw score in [-1, 1].
        """
        # Normalize histogram by close
        if ind.close == 0:
            return 0.0
        raw = ind.macd_histogram / (ind.close * 0.01)  # ~1% of price
        return round(max(-1.0, min(1.0, raw)), 4)

    @staticmethod
    def _score_macd_histogram(ind: IndicatorResults) -> float:
        """Score MACD histogram拐头 (for RANGE regime).

        Histogram rising → BUY; falling → SELL.

        Returns:
            Raw score in [-1, 1].
        """
        diff = ind.macd_histogram - ind.macd_histogram_previous
        if ind.close == 0:
            return 0.0
        raw = diff / (ind.close * 0.005)
        return round(max(-1.0, min(1.0, raw)), 4)

    @staticmethod
    def _score_ma_alignment(ind: IndicatorResults) -> float:
        """Score MA alignment.

        bullish → [0.5, 1.0], bearish → [-1.0, -0.5], scaled by MA distance.
        neutral → continuous from MA cross proximity.

        Returns:
            Raw score in [-1, 1].
        """
        if ind.ma_alignment == "bullish":
            if ind.ma_long == 0:
                return 0.5
            diff_pct = (ind.ma_short - ind.ma_long) / ind.ma_long
            return round(0.5 + 0.5 * max(-1.0, min(1.0, diff_pct * 10)), 4)
        elif ind.ma_alignment == "bearish":
            if ind.ma_long == 0:
                return -0.5
            diff_pct = (ind.ma_short - ind.ma_long) / ind.ma_long
            return round(-0.5 + 0.5 * max(-1.0, min(1.0, diff_pct * 10)), 4)
        else:
            # Partial score from MA cross proximity
            if ind.ma_long == 0:
                return 0.0
            diff_pct = (ind.ma_short - ind.ma_long) / ind.ma_long
            return round(max(-1.0, min(1.0, diff_pct * 20)), 4)

    @staticmethod
    def _score_di_diff(ind: IndicatorResults) -> float:
        """Score DI difference (+DI - -DI).

        +DI > -DI → BUY, reverse → SELL.

        Returns:
            Raw score in [-1, 1].
        """
        # Normalize: di_diff of 20 = full signal
        raw = ind.di_diff / 20.0
        return round(max(-1.0, min(1.0, raw)), 4)

    @staticmethod
    def _score_adx_direction(ind: IndicatorResults) -> float:
        """Score based on ADX directional component.

        Uses +DI/-DI spread as direction signal, scaled by ADX strength.
        ADX below 20 still gives proportional direction (not zero).

        Returns:
            Raw score in [-1, 1].
        """
        scale = min(1.0, ind.adx_14 / 20.0)
        raw = ind.di_diff / 25.0 * scale
        return round(max(-1.0, min(1.0, raw)), 4)

    @staticmethod
    def _score_rsi(ind: IndicatorResults, use_extreme: bool = False) -> float:
        """Score RSI.

        Standard: RSI < 30 = oversold (BUY), RSI > 70 = overbought (SELL)
        Trend mode: RSI < 40 = BUY, RSI > 60 = SELL (trend-following)

        Args:
            ind: Indicator results.
            use_extreme: If True, use wider extreme thresholds.

        Returns:
            Raw score in [-1, 1].
        """
        rsi = ind.rsi_14

        if use_extreme:
            # Range: wider thresholds
            if rsi < 25:
                return 1.0
            elif rsi < 35:
                return (35 - rsi) / 10.0  # 0.0 → 1.0
            elif rsi > 75:
                return -1.0
            elif rsi > 65:
                return -(rsi - 65) / 10.0  # 0.0 → -1.0
            else:
                return 0.0
        else:
            # Trend: normal thresholds
            if rsi < 30:
                return 1.0
            elif rsi < 50:
                return (50 - rsi) / 20.0
            elif rsi > 70:
                return -1.0
            elif rsi > 50:
                return -(rsi - 50) / 20.0
            else:
                return 0.0

    @staticmethod
    def _score_rsi_extreme(ind: IndicatorResults) -> float:
        """Score RSI for extreme reversal signals (RANGE regime).

        RSI < 20 → strong BUY; RSI > 80 → strong SELL.

        Returns:
            Raw score in [-1, 1].
        """
        rsi = ind.rsi_14
        if rsi < 20:
            return 1.0
        elif rsi < 30:
            return (30 - rsi) / 10.0
        elif rsi > 80:
            return -1.0
        elif rsi > 70:
            return -(rsi - 70) / 10.0
        else:
            return 0.0

    @staticmethod
    def _score_stoch(ind: IndicatorResults) -> float:
        """Score Stochastic oscillator.

        %K < 20 → oversold (BUY); %K > 80 → overbought (SELL).
        Mid-range 20-80 mapped linearly to [-0.5, 0.5].

        Returns:
            Raw score in [-1, 1].
        """
        k = ind.stoch_k
        if k < 20:
            return (20 - k) / 20.0
        elif k <= 50:
            return (k - 50) / 60.0   # [-0.5, 0]
        elif k <= 80:
            return (k - 50) / 60.0   # [0, 0.5]
        else:
            return -(k - 80) / 20.0

    @staticmethod
    def _score_stoch_extreme(ind: IndicatorResults) -> float:
        """Score Stochastic for extreme reversals (RANGE regime).

        Returns:
            Raw score in [-1, 1].
        """
        k = ind.stoch_k
        if k < 10:
            return 1.0
        elif k < 25:
            return (25 - k) / 15.0
        elif k > 90:
            return -1.0
        elif k > 75:
            return -(k - 75) / 15.0
        else:
            return 0.0

    @staticmethod
    def _score_pct_b(ind: IndicatorResults) -> float:
        """Score Bollinger %b.

        %b < 0.1 → oversold (BUY); %b > 0.9 → overbought (SELL).
        Mid-range 0.1-0.9 mapped linearly to [-0.5, 0.5].

        Returns:
            Raw score in [-1, 1].
        """
        pct = ind.pct_b
        if pct < 0.1:
            return (0.1 - pct) / 0.1
        elif pct <= 0.5:
            return (pct - 0.5) / 0.8   # [-0.5, 0]
        elif pct <= 0.9:
            return (pct - 0.5) / 0.8   # [0, 0.5]
        else:
            return -(pct - 0.9) / 0.1

    # ── Bar Momentum (Fix #2) ────────────────────

    def _score_bar_momentum(self, ind: IndicatorResults) -> float:
        """Score based on single-bar momentum (bar_range / ATR).

        bar_range / ATR > momentum_threshold → strong trend bar.
        Bullish bar (close > open) → positive score (BUY bias).
        Bearish bar (close < open) → negative score (SELL bias).

        Formula:
            ratio = (high - low) / atr_14
            if ratio > threshold:
                score = (ratio - threshold) / threshold, clamped to [-1, 1]
                sign: + for bullish (close > open), - for bearish
            else:
                score = 0.0

        Momentum threshold is read from config (default: 2.0).

        Args:
            ind: IndicatorResults with atr_14, bar_open, close, recent_highs, recent_lows.

        Returns:
            Raw score in [-1, 1].
        """
        if ind.atr_14 <= 0:
            return 0.0

        # Current bar range from recent_highs/recent_lows
        bar_range = ind.recent_highs[-1] - ind.recent_lows[-1] if ind.recent_highs and ind.recent_lows else 0.0
        if bar_range <= 0:
            return 0.0

        ratio = bar_range / ind.atr_14
        threshold = self._momentum_threshold

        if ratio <= threshold:
            return 0.0

        # Strong trend bar — magnitude based on how far above threshold
        magnitude = (ratio - threshold) / threshold
        magnitude = max(-1.0, min(1.0, magnitude))

        # Direction: bullish (close > open) → positive, bearish → negative
        if ind.close > ind.bar_open:
            return round(magnitude, 4)
        elif ind.close < ind.bar_open:
            return round(-magnitude, 4)
        else:
            return 0.0

    # ── Bollinger Volatility (Tier 0: boll_vol) ──

    @staticmethod
    def _score_boll_vol(ind: IndicatorResults) -> float:
        """Score BBW (Bollinger Band Width) volatility direction.

        BBW expanding vs contracting gives a leading signal:
          BBW > MA20 → bands expanding → volatility building → potential breakout.
          BBW < MA20 → bands contracting → squeeze → direction uncertain.

        In TREND regime, expanding bands confirm trend continuation (+0.3 bias).
        In RANGE regime, expanding bands suggest breakout is imminent (+0.2 bias).

        Raw score in [-1, 1], but typically stays in [-0.5, 0.5].
        """
        if ind.bbw_ma20 <= 0:
            return 0.0
        ratio = ind.bbw / ind.bbw_ma20
        # 1.0 = equilibrium, >1.2 = expanding, <0.8 = contracting
        scaled = (ratio - 1.0) * 2.5  # 1.0→0, 1.2→0.5, 0.8→-0.5
        return round(max(-1.0, min(1.0, scaled)), 4)

    # ── Stochastic Frequency (Tier 0: stoch_freq) ──

    @staticmethod
    def _score_stoch_freq(ind: IndicatorResults) -> float:
        """Score stochastic oscillation frequency (choppy vs trending proxy).

        Uses the %K-%D spread as a proxy for crossover velocity:
          |%K - %D| large → %K is far ahead of %D → momentum building → trending.
          |%K - %D| small → lines tight → oscillating in place → choppy.

        Positive score = tending toward extreme (momentum), bias toward
        current direction. In RANGE, a high freq (stoch at extreme) boosts
        reversal signals; in TREND, a low freq (stoch stuck) confirms trend.

        Raw score in [-1, 1].
        """
        spread = ind.stoch_k - ind.stoch_d
        # Normalize: spread of 15 = full signal (stoch at 85/70 vs 50/50)
        raw = spread / 15.0
        # Direction bias: if stoch_k > 50 (bullish zone), flip sign
        if ind.stoch_k > 50:
            raw = -raw
        return round(max(-1.0, min(1.0, raw)), 4)

    # ── Threshold Computation ───────────────────

    def _compute_threshold(self, regime: RegimeResult) -> float:
        """Single authoritative score gate — P2b fix (defect 6).

        The base threshold is ``scoring.min_score_threshold`` (config-driven,
        default 0.15). Regime normally only changes indicator WEIGHTS, but
        P1-2 adds one exception: TREND regime (empirically worst win rate,
        50%) gets a raised effective threshold so weak TREND signals are
        filtered out.

        Args:
            regime: Regime result.

        Returns:
            The effective threshold value.
        """
        base = self._min_score_threshold
        if regime.regime == Regime.TREND:
            base = max(base, self._trend_min_score)
        return round(base, 4)

    # ── Cooldown Logic ──────────────────────────

    def compute_cooldown(
        self,
        regime: Regime,
        pre_score: float,
        direction: str,
        last_direction: str,
        vol_ratio: float = 1.0,
        bar_seconds: int = 300,
    ) -> int:
        """Compute cooldown in seconds based on regime, signal quality, and live config.

        P1: when volatility-adaptive cooldown is enabled, the base cooldown is
        scaled by relative band width (vol_ratio = bbw / bbw_ma20):
          high vol (vol_ratio > 1) -> shorter cooldown (re-enter trends faster)
          low vol  (vol_ratio < 1) -> longer cooldown (reduce choppy over-trading)
        Scaling is clipped to +/-30% to stay safe. vol_ratio defaults to 1.0,
        preserving exact legacy behaviour when not supplied.

        P2: time-frame-aware cooldown multiplier — M1 shrinks cooldown 5×,
        H1 grows 12×, clamped to [0.3, 3.0] relative to M5 (300 s).
        bar_seconds defaults to 300 (M5), preserving legacy behaviour.
        """
        # Base cooldown by regime
        if regime == Regime.PRE_TREND:
            base = self._pretrend_cool
        elif regime == Regime.TREND:
            same_dir = direction == last_direction
            if same_dir and pre_score > self._trend_bypass_score:
                base = 0
            elif same_dir and pre_score > 0.50:
                base = self._trend_mid_cool
            elif not same_dir:
                base = self._trend_reverse_cool
            else:
                base = self._trend_cool
        elif regime == Regime.TREND_FADE:
            base = self._fade_cool
        elif regime == Regime.RANGE:
            base = self._range_cool
        else:  # NEUTRAL
            base = self._neutral_cool

        # P1 — volatility-adaptive scaling
        if self._cooldown_vol_enable:
            mult = 1.0 + max(-0.3, min(0.3, (vol_ratio - 1.0) * self._cooldown_vol_scale))
            base = int(round(base * mult))

        # P2 — time-frame-aware multiplier: M1 shrinks 5×, H1 grows 12×,
        # clamped to [DEFAULT_TF_COOLDOWN_MULTIPLIER_MIN, DEFAULT_TF_COOLDOWN_MULTIPLIER_MAX]
        tf_mult = max(DEFAULT_TF_COOLDOWN_MULTIPLIER_MIN,
                      min(DEFAULT_TF_COOLDOWN_MULTIPLIER_MAX,
                          bar_seconds / DEFAULT_BAR_SECONDS_M5))
        base = int(round(base * tf_mult))

        return base

    # ── Config ──────────────────────────────────

    async def load_config(self) -> None:
        """Load scoring parameters from config_provider."""
        if self._config is None:
            return

        try:
            self._base_threshold = await self._config.get_float("score_threshold", 0.10)
            # P2b: the ONE authoritative gate — matches scheduler's
            # scoring.{tf}.min_score_threshold so the engine gate and the AI
            # dispute gate can no longer disagree.
            self._min_score_threshold = await self._config.get_float(
                "scoring.min_score_threshold", 0.15
            )
            # ADX trading floor — below this ADX, force NO_TRADE (config-driven)
            self._min_adx_for_trade = await self._config.get_float(
                "scoring.min_adx_for_trade", 22.0
            )
            # ── P1-2: TREND regime 专属门槛 ──
            self._trend_min_score = await self._config.get_float(
                "scoring.trend_min_score_threshold", 0.40
            )
            # ── P1-3: 可靠性校准（默认关闭，待留出校验稳定后开启）──
            self._calib_enabled = await self._config.get_bool(
                "scoring.calibration_enabled", False
            )
            self._calib_min_p = await self._config.get_float(
                "scoring.calibration_min_p", 0.50
            )
            self._calib_min_n = await self._config.get_int(
                "scoring.calibration_min_n", 15
            )
            try:
                _cj = await self._config.get_json("scoring.calibration_json", None)
                self._calib = _cj.get("buckets") if isinstance(_cj, dict) else None
            except Exception:
                self._calib = None
            # ── T1a: 权重方案配置化（外部可调参）──
            # 从 scoring.weight_schemes_json 加载 5 套权重覆盖默认；
            # 缺失或格式错误则保留模块级硬编码默认，绝不致引擎崩溃。
            try:
                _wsj = await self._config.get_json("scoring.weight_schemes_json", None)
                if isinstance(_wsj, dict):
                    _loaded = 0
                    for _name in ("TREND_WEIGHTS", "TREND_FADE_WEIGHTS",
                                  "RANGE_WEIGHTS", "NEUTRAL_WEIGHTS",
                                  "PRE_TREND_WEIGHTS"):
                        _scheme = _wsj.get(_name)
                        if isinstance(_scheme, dict) and _scheme:
                            self._weight_schemes[_name] = {
                                str(k): float(v) for k, v in _scheme.items()
                            }
                            _loaded += 1
                    if _loaded:
                        logger.info(
                            "ScoringEngine weight schemes loaded from config (%d/%d)",
                            _loaded, len(self._weight_schemes),
                        )
            except Exception as exc:
                logger.warning("weight_schemes_json load failed: %s (using defaults)", exc)
            self._pretrend_threshold_floor = await self._config.get_float(
                "scoring.pretrend_threshold_floor", 0.05
            )
            self._fade_threshold_ceiling = await self._config.get_float(
                "scoring.fade_threshold_ceiling", 0.15
            )
            self._range_threshold_floor = await self._config.get_float(
                "scoring.range_threshold_floor", 0.05
            )
            self._neutral_threshold_offset = await self._config.get_float(
                "scoring.neutral_threshold_offset", 0.05
            )
            self._trend_strong_adx_threshold = await self._config.get_float(
                "scoring.trend_strong_adx_threshold", 25
            )
            self._trend_strong_threshold_offset = await self._config.get_float(
                "scoring.trend_strong_threshold_offset", -0.05
            )
            # ── Plan B: 反趋势抑制配置 ──
            self._trend_reverse_suppress_factor = await self._config.get_float(
                "scoring.trend_reverse_suppress_factor", 0.20)
            self._strong_trend_block_reverse = await self._config.get_bool(
                "scoring.strong_trend_block_reverse", True)
            self._pretrend_threshold_offset = await self._config.get_float(
                "scoring.pretrend_threshold_offset", -0.08
            )
            self._fade_threshold_offset = await self._config.get_float(
                "scoring.fade_threshold_offset", 0.08
            )
            self._range_threshold_offset = await self._config.get_float(
                "scoring.range_threshold_offset", -0.10
            )
            self._neutral_threshold_ceiling = await self._config.get_float(
                "scoring.neutral_threshold_ceiling", 0.80
            )
            # ── Cooldown config (fail-fast, no hardcoded defaults) ──
            self._pretrend_cool = await self._config.get_int("pretrend_cooldown_seconds")
            self._trend_cool = await self._config.get_int("trend_cooldown_seconds")
            self._trend_bypass_score = await self._config.get_float("trend_same_dir_bypass_score")
            self._trend_mid_cool = await self._config.get_int("trend_same_dir_mid_cooldown")
            self._trend_reverse_cool = await self._config.get_int("trend_reverse_cooldown_seconds")
            self._fade_cool = await self._config.get_int("fade_cooldown_seconds")
            self._range_cool = await self._config.get_int("range_boundary_cooldown_seconds")
            self._neutral_cool = await self._config.get_int("neutral_cooldown_seconds")
            # ── P1: Volatility-adaptive cooldown ──
            self._cooldown_vol_enable = await self._config.get_bool("cooldown_vol_enable")
            self._cooldown_vol_scale = await self._config.get_float("cooldown_vol_scale")
            # ── Bar momentum config (Fix #2) ──
            self._momentum_threshold = await self._config.get_float(
                "market.momentum_threshold", DEFAULT_MARKET_MOMENTUM_THRESHOLD)
            logger.info(
                "ScoringEngine config loaded: base=%.2f min_score=%.2f pretrend_max=%.2f fade_max=%.2f "
                "range_min=%.2f neutral_max=%.2f momentum_threshold=%.1f "
                "cooldown_pretrend=%d trend=%d fade=%d range=%d neutral=%d",
                self._base_threshold, self._min_score_threshold,
                self._pretrend_threshold_floor,
                self._fade_threshold_ceiling,
                self._range_threshold_floor,
                self._neutral_threshold_ceiling,
                self._momentum_threshold,
                self._pretrend_cool, self._trend_cool, self._fade_cool,
                self._range_cool, self._neutral_cool,
            )
        except Exception as exc:
            logger.warning("ScoringEngine config load failed: %s (using defaults)", exc)
