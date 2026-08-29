"""Precision Entry Score (Layer 2) — Phase 0 shadow-mode 精准买点分。

三维合成买点质量分 entry_quality ∈ [0,1]：
    entry_quality = w1·方向对齐度 + w2·微观结构确认 + w3·风险回报位置

⚠️ Phase 0 仅 shadow 模式：本模块**从不改变交易行为**，只计算 entry_quality，
供 scheduler 与现有评分链路并行对比（现有拦截 vs 新买点）。Phase 1 起才由
该分替代 compute_pre_score 的方向/分数产出。

核心创新：把"趋势回踩（TREND_PULLBACK）"从被 lag_momentum 硬阻断的对象，
提升为**核心买点状态**——回踩时动量短暂反向正是买点特征，而非拦截理由。
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


class PrecisionEntryScorer:
    """精准买点分计算器（shadow-only，Phase 0）。"""

    def __init__(self, config_provider: Any = None):
        self._config = config_provider
        # 三维权重（方向对齐 / 微观结构确认 / 风险回报）
        self._w1: float = 0.40
        self._w2: float = 0.40
        self._w3: float = 0.20
        # 风险回报硬门槛（R:R < 此值直接降权）
        self._min_rr: float = 1.2

    async def load_config(self) -> None:
        if self._config is None:
            return
        try:
            # 【2026-08-28 co_source v2 清除】本模块被 HEXP 入场闸门直接依赖
            # （scheduler.py:1997/2000），配置键由 co.v2.* 迁至 hexp.entry.*，消除
            # co_source 命名残留；值经同值双写迁移，行为零变化。
            self._w1 = await self._config.get_float("hexp.entry.weight.align", 0.40)
            self._w2 = await self._config.get_float("hexp.entry.weight.structure", 0.40)
            self._w3 = await self._config.get_float("hexp.entry.weight.rr", 0.20)
            self._min_rr = await self._config.get_float("hexp.entry.min_rr", 1.2)
        except Exception as exc:  # pragma: no cover
            logger.warning("PrecisionEntry config load failed (using defaults): %s", exc)

    # ── 三维分量 ──
    def _direction_alignment(self, score_direction: str, ms_direction: str) -> float:
        """信号方向与微观状态方向的一致度（0~1 连续）。"""
        if score_direction in ("BUY", "SELL") and ms_direction in ("UP", "DOWN"):
            _aligned = (score_direction == "BUY" and ms_direction == "UP") or \
                       (score_direction == "SELL" and ms_direction == "DOWN")
            return 1.0 if _aligned else 0.0
        return 0.5  # 无明确方向（NO_TRADE / RANGE 双向）

    def _microstructure(self, indicators, ms_result) -> Tuple[float, dict]:
        """微观结构确认（核心，替代静态指标加权）。"""
        _state = ms_result.state
        _d = ms_result.details
        if _state.value == "TREND_PULLBACK":
            _dev = _d.get("pullback_depth_atr", 0.0)
            _min = 0.5
            _max = 1.5
            _mid = (_min + _max) / 2.0
            _zone = max(0.0, 1.0 - abs(_dev - _mid) / ((_max - _min) / 2.0 + 1e-6))
            # 反转 K 线：末棒逆趋势且实体小（近似：逆趋势即给分）
            _reversal = 0.8 if _d.get("last_bar_against") else 0.5
            # 趋势结构 intact：更高高点序列（近似用强度）
            _structure = min(1.0, 0.4 + ms_result.strength)
            _score = 0.5 * _zone + 0.3 * _reversal + 0.2 * _structure
            return min(1.0, _score), {"zone": round(_zone, 3), "reversal": _reversal,
                                       "structure": round(_structure, 3)}
        if _state.value == "TREND_ACCEL":
            # 真实加速：结构分 = 0.20 + 0.20·accel_quality（0.20~0.40），弱加速（accel_quality 低）
            # 自动落回 ≈0.30 以下；details 缺 accel_quality 时兜底 0.5 → 0.30（向后兼容，无回归）。
            _aq = float(_d.get("accel_quality", 0.5))
            return min(1.0, 0.20 + 0.20 * _aq), {"note": "ride_trend_no_chase", "accel_quality": round(_aq, 3)}
        if _state.value == "TREND_EXHAUST":
            return 0.20, {"note": "wait_reversal"}
        if _state.value == "REVERSAL":
            return 0.70, {"note": "breakout_flip"}
        # RANGE：贴边程度 + 振荡器极值
        _pct_b = float(getattr(indicators, "pct_b", 0.5) or 0.5)
        _edge = min(1.0, abs(_pct_b - 0.5) * 2.0)
        _stoch = float(getattr(indicators, "stoch_k", 50.0) or 50.0)
        _extreme = 1.0 - abs(_stoch - 50.0) / 50.0
        return min(1.0, 0.6 * _edge + 0.4 * _extreme), {"edge": round(_edge, 3),
                                                         "extreme": round(_extreme, 3)}

    def _risk_reward(self, indicators, score_direction: str) -> Tuple[float, float]:
        """入场价到动态止损与目标（对向结构位）的实时 R:R。"""
        _entry = float(getattr(indicators, "close", 0.0) or 0.0)
        _atr = max(float(getattr(indicators, "atr_14", 0.0) or 0.0), 1e-6)
        _upper = float(getattr(indicators, "boll_upper", _entry) or _entry)
        _lower = float(getattr(indicators, "boll_lower", _entry) or _entry)
        if score_direction == "BUY":
            _stop = min(_lower, _entry - _atr * 1.0)
            _target = max(_upper, _entry + _atr * 1.5)
            if _entry <= _stop:
                return 0.0, 0.0
            _rr = (_target - _entry) / (_entry - _stop)
        elif score_direction == "SELL":
            _stop = max(_upper, _entry + _atr * 1.0)
            _target = min(_lower, _entry - _atr * 1.5)
            if _stop <= _entry:
                return 0.0, 0.0
            _rr = (_entry - _target) / (_stop - _entry)
        else:
            return 0.5, 1.5  # 无方向中性
        _rr = max(0.0, _rr)
        _score = max(0.0, min(1.0, (_rr - 1.0) / 1.5))  # rr=1→0, rr=2.5→1
        return _score, round(_rr, 3)

    def compute(self, indicators, ms_result, h1_context, score_result,
                 live_price: Optional[float] = None) -> Tuple[float, dict]:
        """计算 entry_quality（阴影模式，不改行为）。

        entry_quality = w_align·方向对齐 + w_structure·微观结构 + w_rr·风险回报

        Returns:
            (entry_quality, breakdown)
        """
        _dir = getattr(score_result, "direction", "NO_TRADE") or "NO_TRADE"
        _ms_dir = ms_result.direction
        # ── 三个分量分数（0~1），与权重 self._w1/_w2/_w3 区分命名 ──
        _align = self._direction_alignment(_dir, _ms_dir)
        _structure, _structure_detail = self._microstructure(indicators, ms_result)
        _rr_score, _rr = self._risk_reward(indicators, _dir)

        # ★ 加权求和：权重 × 分量分数（修复旧版「平方 + 权重丢失」bug）
        _eq = self._w1 * _align + self._w2 * _structure + self._w3 * _rr_score
        # 风险回报硬门槛：R:R < min_rr 直接降权（把桥 R:R guard 上移到信号层）
        if _dir in ("BUY", "SELL") and self._min_rr > 0 and _rr < self._min_rr:
            _eq *= 0.6

        _breakdown = {
            "direction": _dir,
            "micro_state": ms_result.state.value,
            "micro_dir": _ms_dir,
            "w_align": round(self._w1, 3),
            "w_structure": round(self._w2, 3),
            "w_rr": round(self._w3, 3),
            "align_score": round(_align, 3),
            "structure_score": round(_structure, 3),
            "rr_score": round(_rr_score, 3),
            "rr": _rr,
            "structure_detail": _structure_detail,
            "entry_quality": round(min(1.0, _eq), 4),
        }
        return min(1.0, _eq), _breakdown
