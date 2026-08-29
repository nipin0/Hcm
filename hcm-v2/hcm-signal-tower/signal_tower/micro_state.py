"""Market Micro-State (HMTS) — Phase 0 shadow-mode state machine.

实时微观状态机，替代粗粒度 regime：在 M5 上连续识别 5 个微观状态
（ATR 归一化，非硬切换），用于 Phase 2 的"核心买点 = 状态 × 微观结构确认
× 风险回报位置"精准买点算法。

⚠️ Phase 0 仅 shadow 模式：本模块**从不改变交易行为**，只计算微观状态 +
自适应门槛 θ，供 scheduler 与现有评分链路并行对比（现有拦截 vs 新买点）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class MicroState(str, Enum):
    """5 个实时微观状态（连续量化，非硬切换）。"""
    TREND_ACCEL = "TREND_ACCEL"       # 趋势加速（不建议追）
    TREND_PULLBACK = "TREND_PULLBACK" # 趋势回踩 ★核心买点
    TREND_EXHAUST = "TREND_EXHAUST"   # 趋势衰竭（等反转确认）
    RANGE = "RANGE"                   # 震荡（边界均值回归）
    REVERSAL = "REVERSAL"             # 反转（新趋势起点）


@dataclass
class MicroStateResult:
    """微观状态识别结果。"""
    state: MicroState = MicroState.RANGE
    direction: str = ""   # 微观状态趋势方向：UP / DOWN / ""
    strength: float = 0.0
    details: dict = field(default_factory=dict)


class MicroStateClassifier:
    """M5 微观状态分类器（shadow-only，Phase 0）。

    输入 duck-typed：indicators / regime_result / h1_context。
    配置经 config_provider 热加载（缺省安全回退到内置默认）。
    """

    def __init__(self, config_provider: Any = None):
        self._config = config_provider
        # ── 单一动态门槛 θ（按状态），趋势回踩最低（鼓励）、衰竭最高（谨慎）──
        self._theta: dict[MicroState, float] = {
            MicroState.TREND_PULLBACK: 0.30,
            MicroState.TREND_ACCEL: 0.45,
            MicroState.TREND_EXHAUST: 0.55,
            MicroState.RANGE: 0.45,
            MicroState.REVERSAL: 0.50,
        }
        # 回踩深度黄金区间（ATR 归一化）
        self._pullback_atr_min: float = 0.5
        self._pullback_atr_max: float = 1.5
        # ── 加速度严格判定（防止横盘被误判 TREND_ACCEL 导致每根 M5 棒都下单）──
        self._accel_strict_enabled: bool = True
        self._accel_lookback: int = 12
        self._accel_er_min: float = 0.35
        self._accel_disp_atr_min: float = 1.0

    async def load_config(self) -> None:
        """热加载阈值配置；异常仅告警，保留上次/默认。"""
        if self._config is None:
            return
        try:
            # 【2026-08-28 co_source v2 清除】本模块虽带 co_source 血统，但已被 HEXP 入场
            # 闸门直接依赖（scheduler.py:1961-2013）。配置键由 co.v2.* 迁至 hexp.entry.*，
            # 消除 co_source 命名残留；值经同值双写迁移，行为零变化。
            self._pullback_atr_min = await self._config.get_float(
                "hexp.entry.pullback_atr_min", 0.5)
            self._pullback_atr_max = await self._config.get_float(
                "hexp.entry.pullback_atr_max", 1.5)
            self._accel_strict_enabled = await self._config.get_bool(
                "hexp.entry.accel_strict_enabled", True)
            self._accel_lookback = int(await self._config.get_float(
                "hexp.entry.accel_lookback", 12))
            self._accel_er_min = await self._config.get_float(
                "hexp.entry.accel_er_min", 0.35)
            self._accel_disp_atr_min = await self._config.get_float(
                "hexp.entry.accel_disp_atr_min", 1.0)
            for _s in MicroState:
                self._theta[_s] = await self._config.get_float(
                    f"hexp.entry.theta.{_s.value}", self._theta[_s])
        except Exception as exc:  # pragma: no cover - 配置缺失时安全回退
            logger.warning("MicroState config load failed (using defaults): %s", exc)

    @property
    def theta_map(self) -> dict[MicroState, float]:
        return dict(self._theta)

    def adaptive_theta(self, state: MicroState, atr_vol_factor: float = 1.0) -> float:
        """状态 + 实时波动率自适应的单一门槛。

        atr_vol_factor 高（波动大）略放宽 θ（鼓励在波动中找买点），低则收紧。
        """
        base = self._theta.get(state, 0.45)
        adj = base * (0.85 + 0.15 * max(0.0, min(2.0, atr_vol_factor)))
        return round(min(0.85, max(0.15, adj)), 3)

    # ── 内部工具 ──
    @staticmethod
    def _regime_value(regime_result) -> str:
        _r = getattr(regime_result, "regime", None)
        if _r is None:
            return ""
        return _r.value if hasattr(_r, "value") else str(_r)

    @staticmethod
    def _efficiency_ratio(closes: list, bars: int) -> float:
        """Kaufman 效率比 ∈ [0,1]：净位移 / 路径长度。

        单边推进（强趋势）→ 高；横盘往返（无方向）→ 低。不依赖 ADX，
        用于横盘防御——ADX 在窄幅高频往返时也会被推高，不能作为趋势判据。
        """
        if len(closes) < 2:
            return 0.0
        n = min(bars, len(closes) - 1)
        if n < 1:
            return 0.0
        window = closes[-(n + 1):]
        net = abs(window[-1] - window[0])
        path = sum(abs(window[i] - window[i - 1]) for i in range(1, len(window)))
        if path <= 1e-9:
            return 1.0 if net > 1e-9 else 0.0
        return max(0.0, min(1.0, net / path))

    @staticmethod
    def _new_extreme(closes: list, trend_dir: str) -> bool:
        """末棒收盘是否顺向突破回看窗口极值（真加速应有新高/新低）。"""
        if len(closes) < 3:
            return False
        last = closes[-1]
        prior = closes[:-1]
        if trend_dir == "UP":
            return last > max(prior)
        if trend_dir == "DOWN":
            return last < min(prior)
        return False

    def _trend_dir(self, regime_result, indicators, h1_context) -> str:
        """综合 regime / MA alignment / H1 得出趋势方向。"""
        _td = getattr(regime_result, "trend_direction", "") or ""
        if _td in ("UP", "DOWN"):
            return _td
        _align = getattr(indicators, "ma_alignment", "neutral")
        if _align == "bullish":
            return "UP"
        if _align == "bearish":
            return "DOWN"
        _h1 = getattr(h1_context, "trend_direction", "") or ""
        if _h1 in ("UP", "DOWN"):
            return _h1
        return ""

    def classify(self, indicators, regime_result, h1_context) -> MicroStateResult:
        """识别当前 M5 微观状态。

        Returns:
            MicroStateResult（state / direction / strength / details）
        """
        _regime = self._regime_value(regime_result)
        _adx = float(getattr(indicators, "adx_14", 0.0) or 0.0)
        _atr = max(float(getattr(indicators, "atr_14", 0.0) or 0.0), 1e-6)
        _close = float(getattr(indicators, "close", 0.0) or 0.0)
        # SMA20 ≈ 动态支撑/压力（替代 EMA21，phase0 shadow 足够）
        _ema = float(getattr(indicators, "boll_middle", _close) or _close)
        _macd_h = getattr(indicators, "macd_histogram", None)
        _macd_hp = getattr(indicators, "macd_histogram_previous", None)
        _closes = list(getattr(indicators, "recent_closes", []) or [])
        _bbw_expanding = bool(getattr(regime_result, "bbw_expanding", False))
        _adx_falling_bars = int(getattr(regime_result, "adx_falling_bars", 0) or 0)
        _breakout = bool(getattr(regime_result, "breakout_20bar", False))
        _vol = float(getattr(regime_result, "vol_factor", 1.0) or 1.0)

        _trend_dir = self._trend_dir(regime_result, indicators, h1_context)
        _h1_dir = getattr(h1_context, "trend_direction", "") or ""

        # ── 衰竭检测（ADX 回落 + 动量衰减 + 顶/底背离）──
        _macd_decaying = (_macd_h is not None and _macd_hp is not None
                           and _macd_h < _macd_hp)
        _divergence = False
        if len(_closes) >= 6:
            if _trend_dir == "UP":
                _price_higher = _closes[-1] > max(_closes[-6:-1])
                _divergence = _price_higher and _macd_decaying
            elif _trend_dir == "DOWN":
                _price_lower = _closes[-1] < min(_closes[-6:-1])
                _divergence = _price_lower and _macd_decaying

        # ── 回踩检测 ──
        if _trend_dir == "UP":
            _deviation = (_ema - _close) / _atr      # >0 价格低于 EMA = 回踩
            _last_bar_against = (_closes[-1] < _closes[-2]) if len(_closes) >= 2 else False
        elif _trend_dir == "DOWN":
            _deviation = (_close - _ema) / _atr
            _last_bar_against = (_closes[-1] > _closes[-2]) if len(_closes) >= 2 else False
        else:
            _deviation = 0.0
            _last_bar_against = False
        _in_pullback_zone = self._pullback_atr_min <= _deviation <= self._pullback_atr_max

        # ── 加速度严格判定（横盘防御：效率比 + 净位移，不依赖 ADX）──
        _er = self._efficiency_ratio(_closes, self._accel_lookback)
        _disp_atr = abs((_closes[-1] - _closes[0]) / _atr) if len(_closes) >= 2 else 0.0
        _new_extreme = self._new_extreme(_closes, _trend_dir)
        _real_accel = (
            (not _last_bar_against)
            and _new_extreme
            and (not _macd_decaying)
            and _er >= self._accel_er_min
            and _disp_atr >= self._accel_disp_atr_min
        )
        _er_q = min(1.0, _er / max(self._accel_er_min, 1e-6))
        _disp_q = min(1.0, _disp_atr / max(self._accel_disp_atr_min, 1e-6))
        _accel_quality = round(0.6 * _er_q + 0.4 * _disp_q, 3)

        _details = {
            "regime": _regime,
            "adx": round(_adx, 2),
            "trend_dir": _trend_dir,
            "h1_dir": _h1_dir,
            "pullback_depth_atr": round(_deviation, 3),
            "in_pullback_zone": _in_pullback_zone,
            "last_bar_against": _last_bar_against,
            "adx_falling_bars": _adx_falling_bars,
            "macd_decaying": _macd_decaying,
            "divergence": _divergence,
            "bbw_expanding": _bbw_expanding,
            "breakout_20bar": _breakout,
            "efficiency_ratio": round(_er, 3),
            "net_disp_atr": round(_disp_atr, 3),
            "new_extreme": _new_extreme,
            "real_accel": _real_accel,
            "accel_quality": _accel_quality,
        }

        # ── 状态裁决 ──
        if _regime == "RANGE" or (_regime in ("NEUTRAL",) and _adx < 28 and not _bbw_expanding):
            _state = MicroState.RANGE
            _direction = ""
            _strength = max(0.0, min(1.0, (30.0 - _adx) / 30.0))
        elif _trend_dir in ("UP", "DOWN") and _adx >= 18:
            if _adx_falling_bars >= 2 and _divergence and _macd_decaying:
                _state = MicroState.TREND_EXHAUST
                _direction = _trend_dir
                _strength = min(1.0, 0.5 + _adx / 50.0)
            elif _in_pullback_zone and _last_bar_against:
                _state = MicroState.TREND_PULLBACK
                _direction = _trend_dir
                _mid = (self._pullback_atr_min + self._pullback_atr_max) / 2.0
                _strength = max(0.3, 1.0 - abs(_deviation - _mid) / self._pullback_atr_max)
            elif _breakout and _h1_dir and _h1_dir != _trend_dir:
                _state = MicroState.REVERSAL
                _direction = _h1_dir
                _strength = min(1.0, 0.4 + _adx / 60.0)
            elif self._accel_strict_enabled and not _real_accel:
                # 横盘/弱加速防御：效率比/净位移不足 → 归 RANGE（不触发下单），
                # 避免横盘被误判 TREND_ACCEL 导致每根 M5 棒都下单。
                _state = MicroState.RANGE
                _direction = ""
                _strength = max(0.0, min(1.0, 1.0 - _er))
                _details["accel_reject"] = (
                    f"er={_er:.3f}<{self._accel_er_min} or "
                    f"disp_atr={_disp_atr:.3f}<{self._accel_disp_atr_min} or "
                    f"no_new_extreme={not _new_extreme}"
                )
            else:
                _state = MicroState.TREND_ACCEL
                _direction = _trend_dir
                _strength = min(1.0, 0.5 + _adx / 60.0)
        else:
            # 震荡兜底（灰色 NEUTRAL 无明确方向）
            _state = MicroState.RANGE
            _direction = ""
            _strength = 0.3

        _details["vol_factor"] = round(_vol, 3)
        return MicroStateResult(state=_state, direction=_direction,
                                strength=round(_strength, 3), details=_details)
