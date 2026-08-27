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
from signal_tower.h1_regime_classifier import (
    H1Context, H1_BULLISH, H1_BEARISH, H1_STRONG_STRENGTH,
)

logger = logging.getLogger(__name__)

# ── P1: H1 权重偏移表（4 态 × 指标键）──
# 偏移量在 M5 基线权重上按 trend_strength 加权叠加，再归一化（各键保底 0.02）。
# 键名必须与 _select_weight_scheme 返回的基线权重键一致（ma_alignment 等 9 键）。
# 最大偏移幅度 ≤ ±0.12（符合 v1.1 约束 ±0.15 内）。
#   BULLISH/BEARISH：升趋势类(ma_alignment/macd)、压反转类(boll/stoch/rsi/boll_vol/stoch_freq)
#   RANGE：压趋势类(ma_alignment/macd)、升均值回归类(boll/stoch/rsi/boll_vol/stoch_freq)
#   TRANSITION：不动（用 M5 自身 NEUTRAL_WEIGHTS）
H1_WEIGHT_OFFSET: dict = {
    H1_BULLISH: {
        "ma_alignment": 0.12, "macd": 0.12, "adx": 0.0,
        "boll": -0.08, "stoch": -0.08, "rsi": -0.08,
        "boll_vol": -0.08, "stoch_freq": -0.08, "bar_momentum": 0.0,
    },
    H1_BEARISH: {
        "ma_alignment": 0.12, "macd": 0.12, "adx": 0.0,
        "boll": -0.08, "stoch": -0.08, "rsi": -0.08,
        "boll_vol": -0.08, "stoch_freq": -0.08, "bar_momentum": 0.0,
    },
    "RANGE": {
        "ma_alignment": -0.06, "macd": -0.06, "adx": 0.0,
        "boll": 0.10, "stoch": 0.10, "rsi": 0.10,
        "boll_vol": 0.10, "stoch_freq": 0.10, "bar_momentum": 0.0,
    },
    "TRANSITION": {
        "ma_alignment": 0.0, "macd": 0.0, "adx": 0.0,
        "boll": 0.0, "stoch": 0.0, "rsi": 0.0,
        "boll_vol": 0.0, "stoch_freq": 0.0, "bar_momentum": 0.0,
    },
}

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

# RANGE (optimized): BOLL king, oscillators dominant, 2026-07-20 重校: 趋势型(macd+adx+ma=0.28)微提
# ── boll 23% > stoch 20% > rsi 16% > ma 9% = macd 12% > bar_momentum 10% > adx 7% (+stoch_freq 3%)
RANGE_WEIGHTS = {
    "ma_alignment": 0.09,
    "macd":         0.12,
    "adx":          0.07,
    "boll":         0.23,
    "bar_momentum": 0.10,
    "stoch":        0.20,
    "rsi":          0.16,
    "stoch_freq":   0.03,
}

# NEUTRAL: balanced blend — 2026-07-20 重校: 趋势型组件(ma+macd+adx=0.50) 上提，
# 原 0.40 偏震荡(0.60)，XAUUSD 常处弱趋势被误标 NEUTRAL 时信号被压制。
# ── ma 18% = macd 18% > adx 14% > boll 12% > stoch 14% > rsi 12% > bar_momentum 8% (+boll_vol 4%)
NEUTRAL_WEIGHTS = {
    "ma_alignment": 0.18,
    "macd":         0.18,
    "adx":          0.14,
    "boll":         0.12,
    "bar_momentum": 0.08,
    "stoch":        0.14,
    "rsi":          0.12,
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
    # 诊断标签字段：实际承载 float（RANGE 位置 bonus 数值）或 str（rsi_overbought /
    # rsi_overheat_suppressed / zone_* / adx_floor 等"最后施加的原因标签"）。
    # 历史注解误标 float，与 str 赋值不一致；已纠正为 Any 以如实反映双语义。
    range_bonus_applied: Any = 0.0

    # ── Bar momentum (Fix #2) ──
    bar_momentum_applied: float = 0.0

    # ── P1 (2026-07-15): canonical suppress reason (fixes B3 — magic 9.99) ──
    # When the signal is blocked, this carries the *real* reason, not a
    # constructed "below_threshold(score<threshold)" string. Downstream code
    # (signal_publisher, dashboard) reads this directly. Empty string means
    # signal passed all gates normally.
    fallback_reason: str = ""
    # ── 2026-07-31: NEUTRAL RSI 均值回归确认标志 ──
    # 当 NEUTRAL 体制 RSI 极值经"第二根方向确认"放行时为 True，
    # 用于豁免 ADX floor 与 neutral_min_score（与 RANGE 均值回归同构）。
    neutral_rsi_confirmed: bool = False

    # ── 2026-07-31: H1 趋势对齐方向（高空/低多）──
    # 由 H1Context 推导：H1 UP→"BUY"(低多) / H1 DOWN→"SELL"(高空) / 未明确→None。
    # H1 仅划定方向禁区，不干涉 M5 入场时机与质量评分；用于规则①禁止逆势、
    # 规则②顺势放行（豁免 M5 体制对顺势方向的额外抬高门槛）。
    h1_bias: Optional[str] = None

    # ── 2026-08-13: hexp 极值动量感知闸门诊断标记 ──
    # extreme_chase: 极值区且 mm 动量仍朝原方向 → 允许极值追单（True）。
    # extreme_support_pullback: 价格已回踩到近期摆动支撑带内，方向交 7 因子重裁。
    extreme_chase: bool = False
    extreme_support_pullback: bool = False

    # ── 2026-08-18: hexp 极值反转护栏诊断标记 ──
    # extreme_reversal_blocked: 顶/底极值区 + 动量减弱且反向 + 长影线三条件齐 → 拦原趋势延续单。
    # upper_wick_ratio / lower_wick_ratio: 最新收盘 bar 的上/下影线占全幅比（∈[0,1]），
    # 供观测与 LightGBM 特征（extreme_reversal）同源性标注。
    extreme_reversal_blocked: bool = False
    upper_wick_ratio: float = 0.0
    lower_wick_ratio: float = 0.0

    # ── 2026-08-25: 极值分层裁决标记 ──
    # extreme_pending: 极值区 + 动量回撤，但该 symbol 已有同向保本持仓（风险已锁）。
    #   此时 hexp 不硬封方向，标记此字段并交由风控保本闸门做最终裁决（放行+轻仓/拦截）。
    #   无保本持仓时极值护栏照常硬封（hexp_extreme_guard），此字段保持 False。
    extreme_pending: bool = False

    # ── 2026-08-27 方案A: hp_floor 观测标记 ──
    # hp_score(hp_100) 低于 hexp.scorecard.hp_floor 时置 True，仅作面板观测标注，
    # 不再强制改写 grade/passed（放行严格由 6 维综合 scorecard_total 决定）。
    is_hp_red: bool = False

    # ── 2026-08-27 周期价格位置（抗 Donchian 通道拉宽稀释）──
    # position_cycle = 长 lookback 滚动极值分位[0,1]（0=贴区间下沿,1=贴上沿）
    # position_z     = 偏离长周期中枢多少个 ATR（趋势中也不被通道稀释）
    # 二者用于发信号前判断"当前价格在周期里的位置"，抑制极值区逆势追单/接刀。
    position_cycle: float = 0.5
    position_z: float = 0.0
    # 2026-08-27 周期位置守卫命中标记：由 pos_cycle/pos_z 触发 NO_TRADE 时置 True，
    # 落库供 SQL 统计命中率（可观测性，不阻断逻辑）。
    cycle_pos_blocked: bool = False
    # 2026-08-27 微动量平滑值（EMA，消抖）：前端画方向箭头若要用 mm 视角，必须用此平滑值，
    # 禁止用 mm_score（原始瞬时值，0 附近高频抖 → 方向闪烁缺陷）。
    mm_smoothed: float = 0.0


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
        # ── P-β: 方向裁定门槛（参数化，替代 L358 硬编码 0.20）──
        # max(buy,sell) >= 此值且有一方略强(abs diff>=0.01)才给 BUY/SELL 方向；
        # 低于此值仍判 NO_TRADE。默认 0.20 与历史行为一致，校准时下调以放开低分信号。
        self._direction_min_score: float = 0.20
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
        # 注意：阈值只作"噪声地板"，真正的趋势内放行/阻断由校准硬闸门
        # (scoring.calibration_hard_gate) 按(体制,分数桶)实测胜率决定。
        # 降到 0.20 让 TREND-low(pre_score<0.3, 校准 p_win≈0.42>平衡线) 能放行，
        # 而亏损的 TREND-mid(0.3-0.5, p_win≈0.39) 由校准硬闸门拦截。
        # ── NEUTRAL 体制专属评分门槛（2026-07-28 新增，替代死键 scoring.M5.min_score_threshold）──
        # NEUTRAL 市评分需达到此值才放行（与全局基准取 max）。co_source 模式下由 _apply_adaptive_gate 优先使用。
        self._neutral_min_score: float = 0.45
        # ── 2026-07-31: NEUTRAL RSI 均值回归功能总开关（默认关闭，灰度启用）──
        # True 时激活 NEUTRAL RSI 极值闸门 + 第二根确认，并连带豁免 ADX floor / neutral_min_score。
        self._neutral_rsi_enabled: bool = False
        # per-symbol 跨 bar 挂起状态：{symbol: "BUY"|"SELL"}，第 1 根极值只挂起、第 2 根确认才成交。
        self._neutral_rsi_pending: dict = {}
        # 【C 组 2026-08-03】一波极值只允许确认一单；RSI 回到中性区后重新武装。
        # 防止"确认→清除→再挂起→再确认"在同一波超卖/超买中连续开同向单
        # （NEUTRAL 体制 cooldown=0，无其他约束）。
        self._neutral_rsi_armed: dict = {}
        # ── P1-3: 可靠性校准（默认关闭，防小样本过拟合）──
        self._calib_enabled: bool = False
        self._calib: Optional[dict] = None
        self._calib_min_p: float = 0.50   # 软折扣阈值：仅当实测 p_win < 此值才折扣
        self._calib_min_n: int = 12       # 且仅当桶样本数 ≥ 此值（稀疏桶忽略）
        # ── 2026-07-25: 校准硬闸门（多而准）── 低胜率桶直接 NO_TRADE
        self._calib_hard_gate: bool = False
        self._calib_gate_p: float = 0.40  # 硬闸门盈亏平衡线(R:R=1.5 → p≥40%)

        # ── Plan B (2026-07-16): 反趋势抑制（根治逆势开仓）──
        # trend_reverse_suppress_factor: 反趋势单的软抑制乘数（默认 0.20）
        # strong_trend_block_reverse: 强趋势(adx≥trend_strong_adx_threshold)完全阻断反向单
        self._trend_reverse_suppress_factor: float = 0.20
        self._strong_trend_block_reverse: bool = True
        self._strong_trend_reverse_penalty: float = 0.30  # [2026-08-04] 强趋势(ADX≥阈值)逆势降分系数（解耦硬阻断）


        # ── H1 主趋势方向门控（2026-07-27 立项，2026-08-04 解耦硬阻断 → 逆势降分）──
        # 旧逻辑：H1 direction_confirmed=True 时硬 NO_TRADE（h1_counter_trend_blocked），
        #   误杀真反转窗口、且无法放行"高确信逆势单"。
        # 新逻辑：逆 H1 一律【降分】(乘性折扣 pre_score)，仅 M5 评分足够高才过 co_source
        #   闸门；力度随 H1 确认度分级(confirmed 更重、未确认较轻)。
        # 总开关 _h1_reverse_block_enabled 语义改为"启用逆 H1 降分"。
        self._h1_reverse_block_enabled: bool = True
        self._h1_reverse_penalty: float = 0.55        # 逆 H1 未确认(direction_confirmed=False)：较轻降分
        self._h1_reverse_penalty_confirmed: float = 0.30  # 逆 H1 已确认：更重降分（抑制真趋势逆做）
        # ── 2026-07-31: H1 方向门控（高空/低多，禁止逆势）──
        # 设计：H1 只输出"趋势对齐方向" h1_bias（UP→低多BUY / DOWN→高空SELL），
        # M5 保留自身入场时机与质量评分，但被 H1 划定的反向禁区限制：
        #  · h1_bias_enabled：总开关（同时控制顺势放行豁免与逆势硬阻断）
        #  · h1_bias_min_strength：H1 强度达到此值才激活 bias（默认0.50，对应 ADX≈21）
        #  低于此值 H1 未明确趋势 → bias=None → H1 完全不干涉 M5（现状行为）。
        self._h1_bias_enabled: bool = True
        self._h1_bias_min_strength: float = 0.50
        # ── 动量同向门控（2026-07-27）：滞后均线主导 + 动量反向 → 不放行 ──
        self._lag_mom_conflict_block: bool = True
        self._lag_mom_conflict_mom_opp: float = 0.10  # 动量反向最小占比阈值(低于此不硬阻断)
        # 滞后组(ma_alignment+adx)占该方向总分的最小占比阈值：高于此且动量反向才硬阻断。
        # 默认 0.40 偏低(趋势市顺趋势 BUY 的 lag_share 结构性≥0.46)，改为可配置键，
        # A 级修复建议 0.55 以恢复顺趋势回踩单、不破坏顶部背离陷阱拦截。
        self._lag_mom_conflict_share: float = 0.40
        # ── 重构方案 Phase1: 强趋势 RSI 过热豁免开关 ──
        # True(默认): 强趋势(ADX≥强趋势反向阻断阈值 且 体制为 TREND/PRE_TREND)中，
        # RSI 极端是健康趋势跟随特征，跳过 overheat 折扣 → 恢复顺势单（修复强趋势不出单）。
        # False: 退回旧行为（任何行情都砍 RSI 极端单）。
        # 可经配置键 scoring.overheat_suppress_in_trend 即时回退。
        self._overheat_suppress_in_trend: bool = True

        # ── 重构方案 Phase 1/2/3 灰度总开关 ──
        # co.v2_enabled=True 时，方向与分数改由 precision_entry/micro_state 产出
        # （见 co_source.apply_v2），默认引擎的三个折扣(overheat/lagging/lag_momentum)
        # 不再参与决策（由 apply_v2 的微观结构裁决替代）。默认 False（关闭）→ 行为不变。
        self._v2_enabled: bool = False

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

        # ── 2026-07-25: RANGE 体制均值回归硬闸门参数 ──
        # 仅当"振荡器极值(stoch 或 rsi) + %b 贴边"才允许反向开仓，过滤
        # RANGE-high 亏单(calibration p_win=0.20)。真突破放弃反向。
        self._range_min_pct_b: float = 0.15            # %b 贴边阈值：BUY<此 / SELL>1-此
        self._range_stoch_extreme: float = 25.0        # stoch %K 超卖<此 / 超买>100-此
        self._range_rsi_extreme_low: float = 35.0      # rsi 超卖<此
        self._range_rsi_extreme_high: float = 65.0     # rsi 超买>此
        self._range_breakout_mult: float = 1.005       # 收盘突破 20 根高低点倍数

        # ── Main API ────────────────────────────────

    def compute_pre_score(
        self,
        indicators: IndicatorResults,
        regime: RegimeResult,
        zone_level: float = 0.0,
        zone_type: str = "",
        zone_strength: int = 0,
        live_adx: Optional[float] = None,
        h1_context: Optional[H1Context] = None,
        signal_production: bool = False,
    ) -> ScoreResult:
        """Compute pre_score and direction from indicators and regime.

        Args:
            indicators: Computed indicator values.
            regime: Regime classification result.
            zone_level: Zone structure price level (Tier 2 — optional).
            zone_type: PIVOT / RESISTANCE / SUPPORT (Tier 2 — optional).
            zone_strength: Zone confidence ms 1-3 (Tier 2 — optional).
            signal_production: True when called from the bar-close signal production
                path (_produce_signal). Only then should NEUTRAL RSI pending state
                be written/consumed. Live score publisher passes False to avoid
                stealing the pending state before bar close.

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

        # ── H1 方向仅作诊断日志（M5 独立决策，与防火墙无关）──
        # 旧 H1 权重偏移(same_mult/reverse_mult/reverse_strong_block)已于 2026-07-20 移除；
        # H1 主趋势防火墙(scoring_engine.py:695)于 2026-07-27 重新加回、独立硬阻断逆势单。
        if (self._h1_reverse_block_enabled and h1_context is not None
                and h1_context.trend_direction in ("UP", "DOWN")):
            logger.debug(
                "H1 context (diagnostic only): regime=%s dir=%s strength=%.2f; M5 decision independent",
                h1_context.regime, h1_context.trend_direction, h1_context.trend_strength,
            )

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
        if abs(score_diff) < 0.01 and max(buy_score, sell_score) < self._direction_min_score:
            result.direction = "NO_TRADE"
            result.pre_score = max(buy_score, sell_score)
        elif score_diff > 0:
            result.direction = "BUY"
            result.pre_score = round(buy_score, 4)
        else:
            result.direction = "SELL"
            result.pre_score = round(sell_score, 4)

        # ── 2026-07-31: H1 趋势对齐方向（高空/低多，禁止逆势）──
        # 只要 H1 给出方向读数(trend_direction∈{UP,DOWN})且强度达标 → 设定 h1_bias：
        #   UP   → "BUY"  (低多：只做多，等回调低点多)
        #   DOWN → "SELL" (高空：只做空，等反弹高空)
        # 不限 regime(BULLISH/BEARISH/RANGE/TRANSITION 均可)，因为用户要求"M5 参考
        # H1 方向"——即便 RANGE 中 H1 仍给出 DOWN 读数也应倾向高空而非低多；真正的
        # "不干涉"留给无任何方向读数或强度低于阈值的情况(bias=None)。
        # bias 仅作"方向禁区"与"顺势放行"依据，不干涉 M5 入场时机/质量评分。
        result.h1_bias = None
        if (self._h1_bias_enabled
                and h1_context is not None
                and h1_context.trend_direction in ("UP", "DOWN")
                and h1_context.trend_strength >= self._h1_bias_min_strength):
            result.h1_bias = ("BUY" if h1_context.trend_direction == "UP"
                              else "SELL")
        if result.h1_bias:
            logger.debug(
                "H1 bias active %s: h1=%s(%.2f,%s) → bias=%s (m5_dir=%s)",
                getattr(regime, "symbol", ""), h1_context.trend_direction,
                h1_context.trend_strength, h1_context.regime,
                result.h1_bias, result.direction,
            )

        # ── H1 主趋势防火墙已在下方(695 行)独立生效：逆 H1 强趋势单会被硬阻断 ──
        # （2026-07-27 重新加回，弥补 2026-07-20 移除旧权重偏移后留下的盲区）
        # 本段落不再做 H1 干预；M5 独立决定方向与分数后，由防火墙统一裁决。

        # ── 2026-07-31: NEUTRAL 体制 RSI 均值回归闸门（复用 RANGE RSI 极值键）──
        # 仅当 neutral_rsi_enabled=True 激活。逻辑：
        #   · 第 1 根 bar：rsi < range_rsi_extreme_low → 挂起 BUY；
        #                 rsi > range_rsi_extreme_high → 挂起 SELL；本根不直接成交。
        #   · 第 2 根 bar：若挂起方向与本根 M5 评分方向一致、且 rsi 仍处极端区
        #     → 确认放行（neutral_rsi_confirmed=True，豁免 ADX floor + neutral_min_score），
        #     并清 pending；否则清 pending（不成交）。
        # 复用 RANGE 的 rsi 极值键(range_rsi_extreme_low/high)保持单一真源，
        # 与 RANGE 同构——均值回归本就在低 ADX 获利，故豁免 ADX floor。
        # signal_production 保证 pending 状态仅在实际发单路径消费/写入，
        # 避免 live score publisher（监控循环）抢占 pending 导致 bar-close 时永远空。
        if (self._neutral_rsi_enabled
                and signal_production
                and result.regime == Regime.NEUTRAL):
            # 【修复 2026-08-03】移除原 "result.direction != NO_TRADE" 前置：
            # RSI 极值是领先信号，常在 M5 自身尚未转方向(NO_TRADE)时即触发，
            # 原前置导致均值回归永远挂不起 pending → 功能失效(死代码)。
            # 现 RSI 极值时直接以极值方向作为候选方向(_ncand)，使下游可见。
            _nsym = getattr(regime, "symbol", "")
            _nrsi = indicators.rsi_14
            _nlow = self._range_rsi_extreme_low
            _nhigh = self._range_rsi_extreme_high
            if _nrsi is None:
                _ncand = None
            elif _nrsi < _nlow:
                _ncand = "BUY"
            elif _nrsi > _nhigh:
                _ncand = "SELL"
            else:
                _ncand = None
            _npending = self._neutral_rsi_pending.get(_nsym)
            if _ncand is None:
                # 非极值 → 清挂起，避免旧挂起在下根误触发；
                # 【C 组】同时重新武装：RSI 回到中性区后才允许下一波极值再确认
                self._neutral_rsi_pending.pop(_nsym, None)
                self._neutral_rsi_armed[_nsym] = True
            elif not self._neutral_rsi_armed.get(_nsym, True):
                # 【C 组】本波极值已确认一单且 RSI 未回中性区 → 不再挂起/确认
                pass
            elif _npending is None:
                # 第 1 根：RSI 极值 → 以均值回归方向作为候选方向并挂起。
                # 即便 M5 尚未给出方向(NO_TRADE)，RSI 极值本就是领先信号，
                # 故直接把 direction 设为候选方向；本根仍受 NEUTRAL 评分门槛
                # 约束不成交，仅挂起等待第 2 根确认。
                self._neutral_rsi_pending[_nsym] = _ncand
                if result.direction == "NO_TRADE":
                    result.direction = _ncand
                logger.debug(
                    "NEUTRAL RSI pending set: %s %s (rsi=%.1f)",
                    _nsym, _ncand, _nrsi,
                )
            else:
                # 第 2 根：挂起方向一致 + 本根 RSI 仍极端 + 方向一致 → 确认放行
                if result.direction == "NO_TRADE":
                    result.direction = _ncand
                if _npending == _ncand and _npending == result.direction:
                    result.neutral_rsi_confirmed = True
                    # 【C 组】本波极值已用 → 解除武装，RSI 回中性区后才可再确认
                    self._neutral_rsi_armed[_nsym] = False
                    result.threshold_passed = True
                    result.fallback_reason = (
                        f"neutral_rsi_confirmed({_ncand},rsi={_nrsi:.1f})"
                    )
                    logger.info(
                        "NEUTRAL RSI 2-bar confirmed: %s %s (rsi=%.1f)",
                        _nsym, _ncand, _nrsi,
                    )
                # 每对极值只触发一次，清挂起
                self._neutral_rsi_pending.pop(_nsym, None)

        # ── 2026-07-25: RANGE 体制均值回归硬闸门 ──
        # 仅在"振荡器极值(stoch 或 rsi) + %b 贴边"才允许反向开仓，
        # 过滤 RANGE-high 亏单(calibration p_win=0.20，模型给高分却把震荡
        # 误判成突破去追导致亏损)。真突破(收盘突破 20 根高低点)放弃反向，
        # 避免把真突破误判成均值回归逆势。
        if result.regime == Regime.RANGE and result.direction != "NO_TRADE":
            _dir = result.direction
            _pct = indicators.pct_b
            _rsi = indicators.rsi_14
            _k = indicators.stoch_k
            _close = indicators.close
            _rhigh = indicators.recent_highs[-1] if indicators.recent_highs else 0.0
            _rlow = indicators.recent_lows[-1] if indicators.recent_lows else 0.0
            # 真突破：收盘突破 20 根高低点 × mult → 放弃反向均值回归
            _break_up = (_rhigh > 0 and _close > _rhigh * self._range_breakout_mult)
            _break_dn = (_rlow > 0 and _close < _rlow / self._range_breakout_mult)
            if _dir == "SELL" and _break_up:
                result.direction = "NO_TRADE"
                result.pre_score = round(max(raw_buy_score, raw_sell_score), 4)
                result.threshold_passed = False
                result.fallback_reason = (
                    f"range_breakout_up(giveup_reverse, close={_close:.2f} > high*{self._range_breakout_mult}={_rhigh*self._range_breakout_mult:.2f})"
                )
                logger.debug("RANGE gate: %s", result.fallback_reason)
                return result
            if _dir == "BUY" and _break_dn:
                result.direction = "NO_TRADE"
                result.pre_score = round(max(raw_buy_score, raw_sell_score), 4)
                result.threshold_passed = False
                result.fallback_reason = (
                    f"range_breakout_dn(giveup_reverse, close={_close:.2f} < low/{self._range_breakout_mult}={_rlow/self._range_breakout_mult:.2f})"
                )
                logger.debug("RANGE gate: %s", result.fallback_reason)
                return result
            # 极值 + 贴边 才能发（C 方案 2026-07-27：RSI 极值通道独立）
            # RSI 超买/超卖即放行（豁免 %b 贴边）；仅当 RSI 未极值时，才维持
            # "贴边 + (stoch 或 rsi 极值)"双条件，避免"价格在中轨、stoch 刚超买
            # 就逆势接刀"。既恢复"RSI#68 sell / RSI<35 buy"的震荡反转语义，
            # 又保留 stoch 单通道的贴边保护。
            if _dir == "BUY":
                _edge = _pct < self._range_min_pct_b
                _stoch_extreme = _k < self._range_stoch_extreme
                _rsi_extreme = _rsi < self._range_rsi_extreme_low
            else:  # SELL
                _edge = _pct > (1.0 - self._range_min_pct_b)
                _stoch_extreme = _k > (100.0 - self._range_stoch_extreme)
                _rsi_extreme = _rsi > self._range_rsi_extreme_high
            if not _rsi_extreme and not (_edge and (_stoch_extreme or _rsi_extreme)):
                result.direction = "NO_TRADE"
                result.pre_score = round(max(raw_buy_score, raw_sell_score), 4)
                result.threshold_passed = False
                result.fallback_reason = (
                    f"range_no_extreme({_dir} need pct_b edge="
                    f"{'<' if _dir == 'BUY' else '>'}{self._range_min_pct_b} "
                    f"& stoch/rsi extreme)"
                )
                logger.debug("RANGE gate: %s", result.fallback_reason)
                return result
            logger.debug(
                "RANGE gate passed: %s pct_b=%.2f rsi=%.1f stoch_k=%.1f",
                _dir, _pct, _rsi, _k,
            )

        # ── Audit 2026-07-14: RSI overheat suppression ──
        # When RSI ≥ 65 (overbought) and direction=BUY, suppress pre_score.
        # When RSI ≤ 35 (oversold) and direction=SELL, suppress pre_score.
        # This prevents buying at the top of a rally (like signal 1104362).
        rsi = indicators.rsi_14
        # ── A+B 折扣链重构（2026-07-20 修复"好时机不下单"）──
        # 原实现把 RSI/滞后/共识/反向 等软折扣顺序乘性叠加，健康趋势单被
        # 层层打到 <0.10。现改为：收集所有软折扣因子，只乘"最严重单项"(min)
        # 避免多重叠加归零；并把过度激进的因子放宽（B）。
        _disc = 1.0
        _disc_reason = ""
        # ── RSI 极端点位压制：方向对但入场点位差时减分，但不完全拒绝 ──
        # ×0.5(65-67/35-32) vs ×0.3(≥68/≤32)：阈值由 58/42 放宽到 65/35
        #   （58-64 BUY 是健康强趋势特征，不再 ×0.5 误伤）。
        # 重构方案 Phase1: 强趋势中 RSI 极端是健康趋势跟随特征，不砍 → 恢复顺势单。
        # 仅在"非（豁免开启 且 强趋势）"时才施加 overheat 折扣。
        _strong_trend = (
            indicators.adx_14 >= self._trend_strong_adx_threshold
            and regime.regime in (Regime.TREND, Regime.PRE_TREND)
        )
        if not self._v2_enabled and not (self._overheat_suppress_in_trend and _strong_trend):
            if result.direction == "BUY" and rsi >= 65:
                if rsi >= 68:
                    factor = 0.3
                else:
                    factor = 0.5
                result.range_bonus_applied = f"rsi_overbought({rsi:.0f})x{factor}"
                if factor < _disc:
                    _disc = factor
                    _disc_reason = f"rsi_overbought({rsi:.0f})x{factor}"
            elif result.direction == "SELL" and rsi <= 35:
                if rsi <= 32:
                    factor = 0.3
                else:
                    factor = 0.5
                result.range_bonus_applied = f"rsi_oversold({rsi:.0f})x{factor}"
                if factor < _disc:
                    _disc = factor
                    _disc_reason = f"rsi_oversold({rsi:.0f})x{factor}"
        else:
            # 强趋势豁免：记录但不折扣，便于观测
            if result.direction == "BUY" and rsi >= 65:
                result.range_bonus_applied = f"rsi_overheat_suppressed({rsi:.0f})"
            elif result.direction == "SELL" and rsi <= 35:
                result.range_bonus_applied = f"rsi_overheat_suppressed({rsi:.0f})"

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
                    factor = 0.80
                if factor != 1.00:
                    if factor > 1.00:
                        # 高共识加成：立即乘到 pre_score
                        result.pre_score = round(result.pre_score * factor, 4)
                    elif factor < _disc:
                        # 低共识软折扣：收集到 _disc（最终只乘最严重单项）
                        _disc = factor
                        _disc_reason = f"confluence_low_agree({agree}/{total})"

        # ── P1-1: 滞后指标折扣（直击“高分=低胜率”倒挂根因）──
        # 当信号方向主要由 滞后指标(MA对齐 + MACD柱) 驱动，且 ADX 高（趋势已延伸、
        # 末端风险大）时，对 pre_score 打 0.80 折扣。实测 TREND_WEIGHTS 含 42% 滞后
        # 权重、胜率仅 50%；高分追末端→大回撤(MAE 20点)。
        if not self._v2_enabled and result.direction != "NO_TRADE" and result.pre_score > 0 and result.component_scores:
            _idx = 0 if result.direction == "BUY" else 1
            _chosen = sum(max(0.0, c[_idx]) for c in result.component_scores.values())
            _ma_macd = (
                result.component_scores.get("ma_alignment", (0.0, 0.0))[_idx]
                + result.component_scores.get("macd", (0.0, 0.0))[_idx]
            )
            _lag_share = (_ma_macd / _chosen) if _chosen > 0 else 0.0
            if _lag_share > 0.40 and indicators.adx_14 > 28:
                if 0.90 < _disc:
                    _disc = 0.90
                    _disc_reason = (
                        f"lagging_discount(adx={indicators.adx_14:.1f},"
                        f"lag_share={_lag_share:.2f})"
                    )

        # ── 动量同向门控（2026-07-27）：滞后均线主导 + 动量反向 → 硬阻断 ──
        # 根治“均线/ADX 滞后 + 体制误判”导致的逆实际走势单（如 M5 已三连阴、
        # 仍由 adx方向+ma_alignment 撑出 BUY）。当信号方向的主要功劳来自滞后
        # 趋势/均线组(ma_alignment+adx)，而即时动量组(bar_momentum+macd+rsi)
        # 给出反向时，要求动量至少不反向才放行，否则 NO_TRADE。
        # 重构方案 Phase1: 动量同向门控支持"硬阻断/软折扣"双模式。
        # 硬阻断(默认保留): 滞后组主导+动量反向 → NO_TRADE（保留顶部背离陷阱拦截）。
        # 软折扣(关闭 scoring.lag_momentum_conflict_block): 顺趋势回踩单不再被杀，
        # 仅打 ×0.70 折扣，由下游 co_source 门槛裁决 → 恢复最佳回踩买点。
        if (not self._v2_enabled) and (result.direction != "NO_TRADE" and result.component_scores):
            _idx = 0 if result.direction == "BUY" else 1
            _opp = 1 - _idx
            _chosen = sum(max(0.0, c[_idx]) for c in result.component_scores.values())
            _lag = sum(
                max(0.0, result.component_scores.get(k, (0.0, 0.0))[_idx])
                for k in ("ma_alignment", "adx")
            )
            _mom_opp = sum(
                max(0.0, result.component_scores.get(k, (0.0, 0.0))[_opp])
                for k in ("bar_momentum", "macd", "rsi")
            )
            if _chosen > 0 and (_lag / _chosen) > self._lag_mom_conflict_share and _mom_opp > self._lag_mom_conflict_mom_opp:
                if self._lag_mom_conflict_block:
                    result.direction = "NO_TRADE"
                    # 【P0-2b】硬拦截须清除 RSI 确认标记，防止残留被下游误放行
                    result.neutral_rsi_confirmed = False
                    result.pre_score = round(max(raw_buy_score, raw_sell_score), 4)
                    result.threshold_passed = False
                    result.fallback_reason = (
                        f"lag_momentum_conflict(lag_share={_lag/_chosen:.2f},"
                        f"mom_opp={_mom_opp:.3f})"
                    )
                    logger.info(
                        "Lag-momentum conflict BLOCKED %s: dir=%s lag_share=%.2f mom_opp=%.3f",
                        getattr(regime, "symbol", ""), result.direction,
                        _lag / _chosen, _mom_opp,
                    )
                    return result
                else:
                    # 软折扣：保留方向，仅降分（恢复顺趋势回踩买点）
                    if 0.70 < _disc:
                        _disc = 0.70
                        _disc_reason = (
                            f"lag_momentum_soft(lag_share={_lag/_chosen:.2f},"
                            f"mom_opp={_mom_opp:.3f})"
                        )

        # ── P1-3: 可靠性校准（默认关闭，防小样本过拟合）──
        # calibration_enabled 总开关开启后：
        #   · calibration_hard_gate=False（默认）→ 低胜率桶 pre_score×0.85 软折扣；
        #   · calibration_hard_gate=True        → 低胜率桶(p_win<gate_p)直接 NO_TRADE
        #     （硬闸门，实现"多而准"：只放(体制,分数桶)实测胜率≥盈亏平衡线的桶）。
        # 仅当桶样本充足(n≥min_n)才信任其 p_win；趋势类体制的"无校准桶"视为未知
        # edge 硬阻断，NONE/NEUTRAL/RANGE 的无校准桶则放行（这些体制本就更安全）。
        # 失败安全：硬闸门已开但校准表缺失 → 趋势类体制直接阻断，避免放任全部趋势单。
        _trend_regimes = (Regime.TREND, Regime.PRE_TREND, Regime.TREND_FADE)
        if (self._calib_enabled and result.direction != "NO_TRADE"
                and result.pre_score > 0):
            if self._calib_hard_gate and not self._calib:
                # 硬闸门已开但校准表缺失 → 趋势类体制失败安全阻断
                if regime.regime in _trend_regimes:
                    result.direction = "NO_TRADE"
                    result.pre_score = round(max(raw_buy_score, raw_sell_score), 4)
                    result.threshold_passed = False
                    result.fallback_reason = "calib_missing(hard_gate_on_no_table)"
                    logger.debug("Calibration hard-gate (fail-safe): %s", result.fallback_reason)
                    return result
            if self._calib:
                _regime = result.regime.value
                _b = ("low" if result.pre_score < 0.3
                      else "mid" if result.pre_score < 0.5 else "high")
                _cell = self._calib.get(_regime, {}).get(_b)
                # 仅当该(体制,桶)有充足样本才信任其 p_win；缺失/稀疏桶不拦截，
                # 放行给其他闸门（注意：PRE_TREND 等在校准表无条目，若硬阻断会
                # 误杀趋势起点单，故缺失桶一律放行）。
                if _cell is not None and _cell.get("n", 0) >= self._calib_min_n:
                    _p = _cell.get("p_win", 1.0)
                    if self._calib_hard_gate and _p < self._calib_gate_p:
                        # 硬闸门：低胜率桶直接阻断
                        result.direction = "NO_TRADE"
                        # 【P0-2b】硬拦截须清除 RSI 确认标记
                        result.neutral_rsi_confirmed = False
                        result.pre_score = round(max(raw_buy_score, raw_sell_score), 4)
                        result.threshold_passed = False
                        result.fallback_reason = (
                            f"calib_block({_regime},{_b},p={_p:.2f},n={_cell['n']})"
                        )
                        logger.debug("Calibration hard-gate: %s", result.fallback_reason)
                        return result
                    if not self._calib_hard_gate and _p < self._calib_min_p:
                        # 软折扣（旧行为，hard_gate 关闭时生效）
                        if 0.85 < _disc:
                            _disc = 0.85
                            _disc_reason = (
                                f"calib_discount({_regime},{_b},"
                                f"p={_p:.2f},n={_cell['n']})"
                            )

        # ── Plan B (2026-07-16): 反趋势抑制门控 — 根治逆势开仓 ──
        # 当体制已判定为趋势类(TREND/PRE_TREND/TREND_FADE)且方向已明确时，
        # 若评分方向与体制方向相反 → 降分(乘性折扣 pre_score，经 _disc 累加器)：
        #   · 强趋势(adx ≥ trend_strong_adx_threshold) 且启用强趋势降分
        #     → 更重降分 _strong_trend_reverse_penalty（默认0.30，仅 M5 高分才过阈）
        #   · 否则 → pre_score *= trend_reverse_suppress_factor（默认 0.40，软抑制）
        # 解耦 2026-08-04：原"强趋势直接 NO_TRADE"硬阻断改为降分，避免误杀真反转。
        # trend_direction 由 RegimeClassifier 综合 DI/MA/突破/价格位置投票得出。
        _td = getattr(regime, "trend_direction", "") or ""
        if (result.direction != "NO_TRADE"
                and regime.regime in _trend_regimes
                and _td in ("UP", "DOWN")):
            _reverse = (_td == "DOWN" and result.direction == "BUY") or (
                _td == "UP" and result.direction == "SELL")
            if _reverse:
                _adx = live_adx if live_adx is not None else indicators.adx_14
                if self._strong_trend_block_reverse and _adx >= self._trend_strong_adx_threshold:
                    # 解耦硬阻断 → 强趋势逆势降分（更重），仅 M5 高分才过 co_source 闸门
                    _pen = self._strong_trend_reverse_penalty
                    if _pen < _disc:
                        _disc = _pen
                        _disc_reason = (
                            f"strong_trend_reverse_downgrade(td={_td},"
                            f"adx={_adx:.1f}>={self._trend_strong_adx_threshold:.0f},"
                            f"pen={_pen:.2f})"
                        )
                # 软抑制：强趋势阻断未启用，或 ADX 未达强趋势阈值时的兜底降分
                elif self._trend_reverse_suppress_factor < _disc:
                    _disc = self._trend_reverse_suppress_factor
                    _disc_reason = (
                        f"reverse_suppress(td={_td},"
                        f"factor={self._trend_reverse_suppress_factor:.2f})"
                    )

        # ── H1 主趋势方向门控：逆 H1 降分（解耦硬阻断，2026-08-04）──
        # 独立于 M5 体制：当 H1 已明确方向(bias 激活)且 M5 评分反向 → 【降分】处理，
        # 仅 M5 评分足够高才过 co_source 闸门；力度随 H1 确认度分级：
        #   · direction_confirmed=True（H1 方向被近期收盘坐实）→ 更重降分
        #     _h1_reverse_penalty_confirmed（默认0.30）
        #   · 未确认（震荡/whip）→ 较轻降分 _h1_reverse_penalty（默认0.55）
        # 清 neutral_rsi_confirmed 防止 :934 早返回绕过降分（修复隐藏 bug）。
        # 总开关 _h1_reverse_block_enabled 语义改为"启用逆 H1 降分"。
        if (result.direction != "NO_TRADE"
                and self._h1_reverse_block_enabled
                and result.h1_bias is not None
                and h1_context is not None
                and h1_context.trend_direction in ("UP", "DOWN")):
            _h1_reverse = (h1_context.trend_direction == "UP"
                           and result.direction == "SELL") or (
                h1_context.trend_direction == "DOWN"
                and result.direction == "BUY")
            if _h1_reverse:
                # 逆 H1 一律清除 RSI 均值回归豁免标记（否则 :934 早返回绕过降分）
                result.neutral_rsi_confirmed = False
                _pen = (self._h1_reverse_penalty_confirmed
                        if h1_context.direction_confirmed
                        else self._h1_reverse_penalty)
                if _pen < _disc:
                    _disc = _pen
                    _disc_reason = (
                        f"h1_reverse_downgrade(h1_dir={h1_context.trend_direction},"
                        f"h1_strength={h1_context.trend_strength:.2f},"
                        f"confirmed={h1_context.direction_confirmed},pen={_pen:.2f})"
                    )
                logger.info(
                    "H1 direction gate DOWNGRADE counter-trend %s: h1=%s(%.2f) "
                    "vs m5=%s — pre_score*%.2f (no hard block)",
                    getattr(regime, "symbol", ""), h1_context.trend_direction,
                    h1_context.trend_strength, result.direction, _pen,
                )

        # ── A: 应用收集到的最严重单项软折扣（min），避免顺序乘性叠加归零 ──
        if _disc < 1.0:
            result.pre_score = round(result.pre_score * _disc, 4)
            if not result.fallback_reason:
                result.fallback_reason = _disc_reason

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
        # 用 max(bar_adx, live_adx)：信号打分基于 bar-close 值，floor 不应用更低的
        # live ADX 去拦截（实测差距 3-11 点，2026-07-17 16/20 信号被误拦）。
        # bar_adx 保证 floor 与评分体系同源，live_adx 做上浮纠正（面板可见 ADX 更高时不拦）。
        adx = max(live_adx, indicators.adx_14) if live_adx is not None else indicators.adx_14
        adx_source = "max(bar,live)" if live_adx is not None else "bar"
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
        #   - NEUTRAL    → 默认维持 floor 阻断（NEUTRAL 不该交易）；
        #                  但若 neutral_rsi_confirmed（已确认的 NEUTRAL RSI 均值回归）
        #                  → 豁免 floor（与 RANGE 同构：均值回归在低 ADX 获利）
        if result.regime == Regime.RANGE:
            # ── H1 感知 floor 移除（2026-07-20）：H1 不再干预 ADX 门槛（指令：H1不做业务干涉）──
            # RANGE ⟺ ADX<22，而 min_adx_for_trade=18；若在此维持 floor 会把"低 ADX 窄幅"
            # 这一最优均值回归环境永久拦死（注释原意图=跳过 floor）。现落地：跳过 floor。
            h1_adx = float(getattr(h1_context, 'adx', 0) or 0) if h1_context else 0.0
            h1_dir = getattr(h1_context, 'trend_direction', '') if h1_context else ''
            logger.debug(
                "H1 read-only (ADX floor): h1_adx=%.1f h1_dir=%s; RANGE floor skipped (0.0)",
                h1_adx, h1_dir,
            )
            effective_adx_min = 0.0  # RANGE 均值回归在低 ADX 获利 → 跳过 ADX floor
        elif (result.regime == Regime.NEUTRAL
              and getattr(result, "neutral_rsi_confirmed", False)):
            # 已确认的 NEUTRAL RSI 均值回归：豁免 ADX floor（低 ADX 区获利）
            effective_adx_min = 0.0
        else:
            effective_adx_min = self._min_adx_for_trade

        # 2026-08-05 (D5): 移除 scoring_engine 对 neutral_rsi_confirmed 的早退豁免。
        # 该豁免现已由 co_source.apply 顶部(282-289)作为唯一权威处理，scoring_engine
        # 早退会"绕过 co_source"造成双裁决假象。此处仅保留 ADX floor 逻辑
        # （neutral_rsi_confirmed 的 effective_adx_min=0.0，不会触发 floor 拦截）。

        if adx < effective_adx_min:
            # blocked: report raw score for diagnostics
            result.pre_score = round(max(raw_buy_score, raw_sell_score), 4)
            result.direction = "NO_TRADE"
            result.threshold_passed = False
            result.range_bonus_applied = f"adx_floor({adx:.1f},{adx_source})"
            result.fallback_reason = (
                f"adx_floor({adx:.1f}<{effective_adx_min},{adx_source}, "
                f"t2t3_boost={t2t3_boost:.4f})"
            )
            return result

        # 评分门槛由 co_source 自适应闸门(pipeline 唯一权威)裁决；
        # 此处仅留全局底作为非 co_source 模式兜底 / 实时快照展示。
        result.threshold = self._compute_threshold(regime, result)
        result.threshold_passed = result.pre_score >= result.threshold

        # 2026-08-05 (D5): 末端兜底——已确认的 NEUTRAL RSI 均值回归在非 co_source
        # 模式(active_model!=co_source)下也必须一致放行（co_source 模式由 apply 顶部
        # 已放行）。保证移除早退豁免后 fallback 行为不变。
        if getattr(result, "neutral_rsi_confirmed", False) and result.direction != "NO_TRADE":
            result.threshold = 0.0
            result.threshold_passed = True

        # 【P0-2 2026-08-26 均值回归 RSI 极值硬校验】震荡/中性市的反向开仓强制 RSI 极值。
        # 根因：RANGE 均值回归闸门只对 regime==RANGE 生效，而 live_override 实时触发时
        # regime 常判 NEUTRAL 或空，中性区反向单漏网（实证 10 条 RANGE 成交单 RSI 全在
        # 28~70 中性区）。此兜底不依赖 regime：凡震荡/中性市（非趋势），反向开仓但
        # 未达反向极值(stoch/rsi) 即拦截，杜绝"中性区追涨杀跌当均值回归"。
        if (result.direction in ("BUY", "SELL")
                and result.regime in (Regime.RANGE, Regime.NEUTRAL)
                and not getattr(result, "neutral_rsi_confirmed", False)):
            _rd = result.direction
            _rrsi = getattr(indicators, "rsi_14", None)
            _rpct = getattr(indicators, "pct_b", None)
            _rk = getattr(indicators, "stoch_k", None)
            _rlow = self._range_rsi_extreme_low
            _rhigh = self._range_rsi_extreme_high
            _rstoch = self._range_stoch_extreme
            _rmin = self._range_min_pct_b
            if _rd == "BUY":
                _rsi_ok = (_rrsi is not None and _rrsi < _rlow)
                _edge_ok = (_rpct is not None and _rpct < _rmin and _rk is not None and _rk < _rstoch)
            else:  # SELL
                _rsi_ok = (_rrsi is not None and _rrsi > _rhigh)
                _edge_ok = (_rpct is not None and _rpct > (1.0 - _rmin) and _rk is not None and _rk > (100.0 - _rstoch))
            if not _rsi_ok and not _edge_ok:
                result.direction = "NO_TRADE"
                result.threshold_passed = False
                result.neutral_rsi_confirmed = False
                result.fallback_reason = (
                    f"range_rsi_hardgate({_rd} need rsi{'<' if _rd=='BUY' else '>'}"
                    f"{_rlow if _rd=='BUY' else _rhigh} or %b edge+rsi/stoch extreme)"
                )
                logger.info(
                    "range_rsi_hardgate block: %s %s rsi=%s pct=%s stoch=%s reason=%s",
                    getattr(regime, "symbol", ""), _rd, _rrsi, _rpct, _rk,
                    result.fallback_reason,
                )

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

    # ── P1: H1 权重偏移（状态判定层 → 执行层权重）──
    def _apply_h1_weight_offset(
        self,
        baseline: dict[str, float],
        h1_regime: str,
        h1_strength: float,
    ) -> dict[str, float]:
        """对 M5 基线权重施加 H1 偏移（v1.1 核心：偏移，非覆盖）。

        偏移 = H1_WEIGHT_OFFSET[h1_regime][k] × h1_strength，叠加到基线后
        归一化；各键保底 0.02，确保极端 H1 态也不会把任何指标权重压到 0。
        """
        offset = H1_WEIGHT_OFFSET.get(h1_regime)
        if not offset or h1_strength <= 0:
            return baseline
        strength = max(0.0, min(1.0, h1_strength))
        result: dict[str, float] = {}
        for k, base in baseline.items():
            off = offset.get(k, 0.0)
            result[k] = max(0.02, base + off * strength)
        total = sum(result.values())
        if total > 0:
            result = {k: round(v / total, 4) for k, v in result.items()}
        return result

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
        # 2026-07-20 重校: 改用 ATR 归一（跨品种尺度无关）。
        # 原 close*0.01（XAUUSD≈3350→分母 33.5）使 M5 直方图(0.5–5) raw≈0.015–0.15，
        # MACD 对总分贡献被结构性压扁→近 0。ATR 与直方图同尺度，比率稳定。
        if ind.close == 0:
            return 0.0
        scale = ind.atr_14 if (ind.atr_14 and ind.atr_14 > 0) else (ind.close * 0.01)
        raw = ind.macd_histogram / scale
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
        # 2026-07-20 重校: 改用 ATR 归一（原 close*0.005→分母 16.75，diff 0.1–2 →
        #  raw≈0.006–0.12，近恒为 0）。直方图差分天然更小，×3 保留区分度。
        scale = ind.atr_14 if (ind.atr_14 and ind.atr_14 > 0) else (ind.close * 0.005)
        raw = (diff / scale) * 3.0
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

    def _compute_threshold(self, regime: RegimeResult,
                           result: Optional["ScoreResult"] = None) -> float:
        """非 co_source 模式 / 实时评分快照的兜底门槛（单一真源在 co_source 自适应闸门）。

        2026-07-31 架构去混乱：生产链路中 co_source.apply 的 _apply_adaptive_gate
        是唯一权威的评分门槛裁决（按行情带 strong/weak/range/shock + 风险偏移 +
        校准因子动态计算），scoring_engine 不再重复做 TREND/NEUTRAL 体制门槛判定，
        避免两套门槛互相覆盖、日志出现 0.15/0.20/0.40/0.45/0.50 混现的双裁决混乱。
        此处仅保留全局底（scoring.min_score_threshold，默认 0.15）作为：
          (1) co_source 未激活时的兜底 pass/fail 判定；
          (2) 实时评分快照面板的原始 floor 展示（真实交易门槛由 co_source 决定）。
        """
        return round(self._min_score_threshold, 4)

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
            # [2026-07-31 C 项·ADX 收敛] 代码默认 22→18，与 M5 强趋势 ADX 统一（反向阻断
            # scoring.trend_strong_adx_threshold、co_source band co.gate.adx_strong 均为 18）。
            self._min_adx_for_trade = await self._config.get_float(
                "scoring.min_adx_for_trade", 18.0
            )
            # ── NEUTRAL regime 专属评分门槛（2026-07-28 新增）──
            self._neutral_min_score = await self._config.get_float(
                "scoring.neutral_min_score_threshold", 0.28
            )  # [2026-08-01] 对齐部署
            # ── 2026-07-31: NEUTRAL RSI 均值回归功能总开关（默认关闭，灰度启用）──
            # 激活后 NEUTRAL 体制复用 RANGE 的 rsi 极值键(range_rsi_extreme_low/high)
            # 做均值回归，并要求"第二根方向确认"才放行。
            self._neutral_rsi_enabled = await self._config.get_bool(
                "scoring.neutral_rsi_enabled", False
            )
            # ── P-β: 方向裁定门槛（替代硬编码 0.20）──
            self._direction_min_score = await self._config.get_float(
                "co.gate.direction_min_score", 0.20
            )
            # ── P1-3: 可靠性校准（默认关闭，待留出校验稳定后开启）──
            self._calib_enabled = await self._config.get_bool(
                "scoring.calibration_enabled", True
            )  # [2026-08-01] 对齐部署
            self._calib_min_p = await self._config.get_float(
                "scoring.calibration_min_p", 0.50
            )
            self._calib_min_n = await self._config.get_int(
                "scoring.calibration_min_n", 12
            )
            # ── 2026-07-25: 校准硬闸门（多而准）──
            self._calib_hard_gate = await self._config.get_bool(
                "scoring.calibration_hard_gate", False)
            self._calib_gate_p = await self._config.get_float(
                "scoring.calibration_gate_p", 0.55)  # [2026-08-01] 对齐部署
            try:
                _cj = await self._config.get_json("scoring.calibration_json", None)
                self._calib = _cj.get("buckets") if isinstance(_cj, dict) else None
            except Exception:
                self._calib = None
            # 2026-08-05 (D10): 校准空数据告警——开启了校准却无数据，评分将退化为无校准，
            # 且极易被误判为"校准已生效"，故显式 CRITICAL 提示运维补 seed。
            if self._calib_enabled and self._calib is None:
                logger.critical(
                    "Calibration ENABLED but no calibration data loaded "
                    "(scoring.calibration_json seeds empty or parse failed) — "
                    "scoring engine will run WITHOUT calibration adjustments"
                )
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
            # [2026-07-31 C 项·ADX 收敛] 代码默认 25→18，与 M5 强趋势 ADX 统一（地板
            # scoring.min_adx_for_trade、co_source band co.gate.adx_strong 均为 18）。
            self._trend_strong_adx_threshold = await self._config.get_float(
                "scoring.trend_strong_adx_threshold", 18
            )
            self._trend_strong_threshold_offset = await self._config.get_float(
                "scoring.trend_strong_threshold_offset", -0.05
            )
            # ── Plan B: 反趋势抑制配置 ──
            self._trend_reverse_suppress_factor = await self._config.get_float(
                "scoring.trend_reverse_suppress_factor", 0.40)  # [2026-08-01] 对齐部署
            self._strong_trend_block_reverse = await self._config.get_bool(
                "scoring.strong_trend_block_reverse", True)
            self._strong_trend_reverse_penalty = await self._config.get_float(
                "scoring.strong_trend_reverse_penalty", 0.30)  # [2026-08-04] 强趋势逆势降分
            # ── H1 逆势降分配置（2026-08-04 解耦硬阻断）→ 逆 H1 一律降分 ──
            self._h1_reverse_block_enabled = await self._config.get_bool(
                "scoring.h1_reverse_block_enabled", True)
            self._h1_reverse_penalty = await self._config.get_float(
                "scoring.h1_reverse_penalty", 0.55)
            self._h1_reverse_penalty_confirmed = await self._config.get_float(
                "scoring.h1_reverse_penalty_confirmed", 0.30)
            # ── 2026-07-31: H1 方向门控（高空/低多，禁止逆势）──
            # h1_bias_enabled 总开关；h1_bias_min_strength 为激活 bias 的 H1 强度门槛。
            self._h1_bias_enabled = await self._config.get_bool(
                "scoring.h1_bias_enabled", True)
            self._h1_bias_min_strength = await self._config.get_float(
                "scoring.h1_bias_min_strength", 0.50)
            self._lag_mom_conflict_block = await self._config.get_bool(
                "scoring.lag_momentum_conflict_block", True)
            self._lag_mom_conflict_mom_opp = await self._config.get_float(
                "scoring.lag_momentum_conflict_mom_opp", 0.10)
            self._lag_mom_conflict_share = await self._config.get_float(
                "scoring.lag_momentum_conflict_share", 0.40)
            self._overheat_suppress_in_trend = await self._config.get_bool(
                "scoring.overheat_suppress_in_trend", True)
            # ── Phase 1/2/3 灰度总开关（默认 False → 关闭，行为与旧完全一致）──
            self._v2_enabled = await self._config.get_bool(
                "co.v2_enabled", False) if self._config is not None else False
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
            # ── Cooldown config (defensive hardcode defaults = current live Redis values) ──
            # [2026-07-24 审计加固] 旧版无默认值：配置整体丢失/Redis 重启未 seed 时退化为 0 →
            # 冷却全关 → 频繁重复同向开单。现用当前生效运行值作兜底，配置缺失也不退化。
            # [2026-08-01] 冷却回退默认对齐部署（生产实际：pretrend/trend/neutral/range=0 关闭，
            # fade=300, trend_reverse=90）。配置缺失时不退化到旧默认而误启冷却阻断成交。
            self._pretrend_cool = await self._config.get_int("pretrend_cooldown_seconds", 0)
            self._trend_cool = await self._config.get_int("trend_cooldown_seconds", 0)
            # 【E 组 P2-1 2026-08-03】尺度修正：pre_score 是 0-1 尺度，
            # 原默认 90.0（0-100 尺度残留）使 bypass 永不命中。配置值同步改为 0.90。
            self._trend_bypass_score = await self._config.get_float("trend_same_dir_bypass_score", 0.90)
            # 注：trend_same_dir_mid_cooldown 存储值为 "0.6"，get_int 解析失败回退 0（无冷却），
            # 故默认取 0 与当前生效行为一致。
            self._trend_mid_cool = await self._config.get_int("trend_same_dir_mid_cooldown", 0)
            self._trend_reverse_cool = await self._config.get_int("trend_reverse_cooldown_seconds", 90)
            self._fade_cool = await self._config.get_int("fade_cooldown_seconds", 300)
            self._range_cool = await self._config.get_int("range_boundary_cooldown_seconds", 0)
            self._neutral_cool = await self._config.get_int("neutral_cooldown_seconds", 0)
            # ── P1: Volatility-adaptive cooldown ──
            self._cooldown_vol_enable = await self._config.get_bool("cooldown_vol_enable", False)
            self._cooldown_vol_scale = await self._config.get_float("cooldown_vol_scale", 0.5)
            # ── Bar momentum config (Fix #2) ──
            self._momentum_threshold = await self._config.get_float(
                "market.momentum_threshold", DEFAULT_MARKET_MOMENTUM_THRESHOLD)
            # ── 2026-07-25: RANGE 均值回归硬闸门参数（外部可调参）──
            self._range_min_pct_b = await self._config.get_float(
                "scoring.range_min_pct_b", 0.15)
            self._range_stoch_extreme = await self._config.get_float(
                "scoring.range_stoch_extreme", 25.0)
            self._range_rsi_extreme_low = await self._config.get_float(
                "scoring.range_rsi_extreme_low", 30.0)  # [2026-08-01] 对齐部署
            self._range_rsi_extreme_high = await self._config.get_float(
                "scoring.range_rsi_extreme_high", 70.0)  # [2026-08-01] 对齐部署
            self._range_breakout_mult = await self._config.get_float(
                "scoring.range_breakout_mult", 1.005)
            logger.info(
                "ScoringEngine config loaded: base=%.2f min_score=%.2f pretrend_max=%.2f fade_max=%.2f "
                "range_min=%.2f neutral_max=%.2f momentum_threshold=%.1f "
                "cooldown_pretrend=%d trend=%d fade=%d range=%d neutral=%d "
                "neutral_rsi_enabled=%s "
                "h1_bias=%s(bias=%.2f) h1_reverse_penalties=%.2f/%.2f "
                "trend_strong_adx=%.1f strong_rev_penalty=%.2f reverse_suppress=%.2f "
                "lag_mom_conflict_share=%.2f mom_opp=%.2f dir_min_score=%.2f "
                "min_adx=%.1f range_pct_b=%.2f stoch=%.1f rsi_low=%.1f breakout=%.3f "
                "calib_enabled=%s calib_loaded=%s",
                self._base_threshold, self._min_score_threshold,
                self._pretrend_threshold_floor,
                self._fade_threshold_ceiling,
                self._range_threshold_floor,
                self._neutral_threshold_ceiling,
                self._momentum_threshold,
                self._pretrend_cool, self._trend_cool, self._fade_cool,
                self._range_cool, self._neutral_cool,
                self._neutral_rsi_enabled,
                self._h1_bias_enabled, self._h1_bias_min_strength,
                self._h1_reverse_penalty, self._h1_reverse_penalty_confirmed,
                self._trend_strong_adx_threshold, self._strong_trend_reverse_penalty,
                self._trend_reverse_suppress_factor,
                self._lag_mom_conflict_share, self._lag_mom_conflict_mom_opp,
                self._direction_min_score,
                self._min_adx_for_trade,
                self._range_min_pct_b, self._range_stoch_extreme, self._range_rsi_extreme_low, self._range_breakout_mult,
                self._calib_enabled, self._calib is not None,
            )
        except Exception as exc:
            logger.warning("ScoringEngine config load failed: %s (using defaults)", exc)
