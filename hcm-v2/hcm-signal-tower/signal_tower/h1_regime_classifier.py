"""H1 Regime Classifier (P0/P1: HMTS 分层多周期状态机 — 状态判定层).

读取 H1 周期的指标（由 ``IndicatorCalculator.compute_all`` 在 H1 klines 上算出），
判定 H1 市场状态（4 态）+ 趋势方向 + 趋势强度，供 ``scoring_engine`` 对 M5 评分
做偏置门控（P0）与权重偏移（P1）。

设计要点：
  · 复用 Plan B / M5 ``RegimeClassifier._compute_trend_direction`` 的"四源投票"方向
    判定逻辑（DI / MA 对齐 / MACD 柱 / 价格位置），作用域切到 H1。
  · 4 态（替代 v1.0 的 8 态）：``BULLISH`` / ``BEARISH`` / ``RANGE`` / ``TRANSITION``。
  · ``trend_strength ∈ [0,1]``，由 H1 ADX 归一化（0→20 映射 0→0.5，20→35 映射 0.5→1.0）。
  · Hysteresis（防 ADX 边界抖动）：进入强趋势需 ADX≥24，回落需 ADX<20。
  · 所有异常 / H1 klines 不足 → 返回 ``None``（上层 fallback 到 M5 only，零新增风险）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from signal_tower.indicator_calculator import IndicatorResults

logger = logging.getLogger(__name__)

# ── H1 状态阈值（针对 H1 周期调校，带 hysteresis 双边界）──
H1_REGIME_ADX_UP = 24.0        # 进入 BULLISH/BEARISH 需 ADX ≥ 24
H1_REGIME_ADX_DOWN = 20.0      # 回落到 RANGE/TRANSITION 需 ADX < 20
H1_REGIME_ADX_MID = 22.0       # 弱趋势区升级为趋势态的临界（hysteresis 上沿附近）
H1_STRONG_STRENGTH = 0.70      # 强趋势门控阈值（P0 反向阻断）
H1_ADX_STRENGTH_FLOOR = 20.0   # 强度归一化下界
H1_ADX_STRENGTH_CEIL = 35.0    # 强度归一化上界

# 4 态（字符串常量，避免与 M5 Regime 枚举耦合）
H1_BULLISH = "BULLISH"
H1_BEARISH = "BEARISH"
H1_RANGE = "RANGE"
H1_TRANSITION = "TRANSITION"

# ── 近期动量 / 确认参数（B/C/E 修复用，针对 H1 周期调校）──
# B: 方向投票新增"近 N 根 H1 收盘斜率"动量票（决定性单向移动才投票，
#    权重 ±2，足以压过其余滞后源，从根上消除方向反转滞后）。
H1_MOMENTUM_BARS = 8        # 取近 8 根 H1 收盘评估斜率（=8 小时）
H1_MOMENTUM_MULT = 2.0      # 斜率须 > mult × 棒间均波动 才算"决定性"
# C: 破 hysteresis 死锁——【A 方案·自适应翻转窗口】不再用固定根数，
#    而按 H1 当前动量强度动态决定确认根数（行情越急、拐点越快确认）。
#    默认值（可被 scoring.h1_flip_* 配置键热覆盖）：
#      intensity ≥ fast → fast_bars 根即翻；≥ mid → mid_bars；否则 slow_bars 兜底。
H1_FLIP_BARS_FAST = 1       # 急动量确认根数（≈1h）
H1_FLIP_BARS_MID = 2        # 中动量
H1_FLIP_BARS_SLOW = 3       # 慢动量兜底（等同旧 H1_FLIP_BARS=3）
H1_FLIP_INTENSITY_FAST = 3.0
H1_FLIP_INTENSITY_MID = 2.0
H1_FLIP_MULT = 1.0
# C: 【B 方案·M5 实时交叉确认】H1 软松动时由 M5（5分钟级，快12倍）提前翻转，
#    不等 H1 收满。防 M5 噪音：必须用"近 N 根净方向 + 强度"，单 bar 不触发。
H1_M5_CONFIRM_ENABLED = True
H1_M5_CONFIRM_BARS = 8       # M5 确认窗口（根）= 40min
H1_M5_CONFIRM_INTENSITY = 1.5  # M5 强度阈值（×vol，防 M5 噪音假翻）
# E: 防火墙确认——仅当"近 N 根 H1 收盘方向与 h1_direction 一致"才硬拦，
#    否则降级软抑制（陈旧方向不再无脑误杀）。
H1_CONFIRM_BARS = 5
H1_CONFIRM_MULT = 0.8


@dataclass
class H1Context:
    """H1 多周期融合上下文，传入 scoring_engine。"""
    regime: Optional[str] = None            # BULLISH/BEARISH/RANGE/TRANSITION/None
    trend_direction: str = ""               # UP / DOWN / ""
    trend_strength: float = 0.0             # [0,1]
    adx: float = 0.0                        # H1 ADX 值
    direction_confirmed: bool = False       # E: 近 N 根 H1 收盘是否确认该方向

    def __bool__(self) -> bool:
        return self.regime is not None


class H1RegimeClassifier:
    """H1 周期市场状态分类器（HMTS 状态判定层）。"""

    def __init__(self, config_provider: Any = None) -> None:
        # 每 symbol 维护上次 regime，用于 hysteresis（防止 ADX 边界抖动反复切换）。
        self._last_regime: dict[str, str] = {}
        self._config = config_provider
        # 翻转参数默认值（load_config 会从 config_provider 热覆盖）。
        self._flip_bars_fast = H1_FLIP_BARS_FAST
        self._flip_bars_mid = H1_FLIP_BARS_MID
        self._flip_bars_slow = H1_FLIP_BARS_SLOW
        self._flip_intensity_fast = H1_FLIP_INTENSITY_FAST
        self._flip_intensity_mid = H1_FLIP_INTENSITY_MID
        self._flip_mult = H1_FLIP_MULT
        self._m5_confirm_enabled = H1_M5_CONFIRM_ENABLED
        self._m5_confirm_bars = H1_M5_CONFIRM_BARS
        self._m5_confirm_intensity = H1_M5_CONFIRM_INTENSITY

    async def load_config(self) -> None:
        """从 config_provider 热加载 H1 翻转参数（缺省兜底，缺失/异常不报错）。"""
        if self._config is None:
            return
        try:
            self._flip_bars_fast = await self._config.get_int(
                "scoring.h1_flip_bars_fast", H1_FLIP_BARS_FAST)
            self._flip_bars_mid = await self._config.get_int(
                "scoring.h1_flip_bars_mid", H1_FLIP_BARS_MID)
            self._flip_bars_slow = await self._config.get_int(
                "scoring.h1_flip_bars_slow", H1_FLIP_BARS_SLOW)
            self._flip_intensity_fast = await self._config.get_float(
                "scoring.h1_flip_intensity_fast", H1_FLIP_INTENSITY_FAST)
            self._flip_intensity_mid = await self._config.get_float(
                "scoring.h1_flip_intensity_mid", H1_FLIP_INTENSITY_MID)
            self._flip_mult = await self._config.get_float(
                "scoring.h1_flip_mult", H1_FLIP_MULT)
            self._m5_confirm_enabled = await self._config.get_bool(
                "scoring.h1_m5_confirm_enabled", H1_M5_CONFIRM_ENABLED)
            self._m5_confirm_bars = await self._config.get_int(
                "scoring.h1_m5_confirm_bars", H1_M5_CONFIRM_BARS)
            self._m5_confirm_intensity = await self._config.get_float(
                "scoring.h1_m5_confirm_intensity", H1_M5_CONFIRM_INTENSITY)
            logger.info(
                "H1RegimeClassifier config loaded | flip fast/mid/slow=%d/%d/%d "
                "intensity fast/mid=%.1f/%.1f m5_confirm=%s bars=%d inten=%.1f",
                self._flip_bars_fast, self._flip_bars_mid, self._flip_bars_slow,
                self._flip_intensity_fast, self._flip_intensity_mid,
                self._m5_confirm_enabled, self._m5_confirm_bars,
                self._m5_confirm_intensity,
            )
        except Exception as exc:
            logger.warning(
                "H1RegimeClassifier config load failed: %s (using defaults)", exc)

    # ── 主入口 ──
    def classify(
        self,
        ind: IndicatorResults,
        symbol: str = "",
        m5_recent_closes: Optional[list] = None,
    ) -> Optional[H1Context]:
        """基于 H1 指标判定 H1 状态。

        Args:
            ind: H1 周期的 ``IndicatorResults``（已用 ``IndicatorCalculator.compute_all`` 计算）。
            symbol: 标的名（用于 hysteresis 状态隔离；可空）。
            m5_recent_closes: 可选，M5 周期近期收盘价序列（用于 B 方案跨周期
                实时交叉确认）。提供时可在 H1 软松动时由 M5 动量提前翻转，
                不等 H1 收满。

        Returns:
            ``H1Context``；异常或数据不足返回 ``None``（上层 fallback 到 M5 only）。
        """
        try:
            adx = float(getattr(ind, "adx_14", 0.0) or 0.0)
            plus_di = float(getattr(ind, "plus_di", 0.0) or 0.0)
            minus_di = float(getattr(ind, "minus_di", 0.0) or 0.0)
            macd = float(getattr(ind, "macd", 0.0) or 0.0)
            macd_hist = float(getattr(ind, "macd_histogram", 0.0) or 0.0)
            ma_align = getattr(ind, "ma_alignment", "") or ""
            close = float(getattr(ind, "close", 0.0) or 0.0)
            highs = getattr(ind, "recent_highs", None) or []
            lows = getattr(ind, "recent_lows", None) or []
            recent_closes = getattr(ind, "recent_closes", None) or []

            # B 方案：M5 实时动量（仅当提供 M5 序列时计算）。
            m5_dir = ""
            m5_int = 0.0
            if m5_recent_closes:
                m5_dir = self._recent_trend_state(
                    m5_recent_closes, self._m5_confirm_bars, self._flip_mult)
                m5_int = self._m5_intensity(
                    m5_recent_closes, self._m5_confirm_bars, self._flip_mult)

            direction = self._vote_direction(
                plus_di, minus_di, macd, macd_hist, ma_align, close, highs, lows,
                recent_closes,
            )
            strength = self._strength(adx)
            regime = self._regime(
                adx, direction, symbol, recent_closes, m5_dir=m5_dir, m5_int=m5_int)
            self._last_regime[symbol] = regime or H1_TRANSITION
            # E: 确认度——近 N 根 H1 收盘净方向是否与 h1_direction 一致。
            direction_confirmed = False
            if direction in ("UP", "DOWN"):
                direction_confirmed = (
                    self._recent_trend_state(recent_closes, H1_CONFIRM_BARS,
                                             H1_CONFIRM_MULT) == direction
                )
            ctx = H1Context(
                regime=regime,
                trend_direction=direction,
                trend_strength=strength,
                adx=adx,
                direction_confirmed=direction_confirmed,
            )
            # [fix] 惯性分支(regime=BEARISH/BULLISH 但本次四源投票方向为空)会保留 regime
            # 却把 trend_direction 留在 ""，导致下游 H1 主趋势防火墙因 trend_direction 缺失
            # 而跳过、逆势单漏过（实证：1510013/1510028 的 h1_regime=BEARISH、strength=0.73
            # 但 h1_trend_direction="" → 防火墙未拦截）。当 regime 已确定趋势态时，方向由
            # regime 唯一决定，此处补全，使防火墙可正常判定反向。
            if ctx.trend_direction == "" and ctx.regime in (H1_BULLISH, H1_BEARISH):
                ctx.trend_direction = "UP" if ctx.regime == H1_BULLISH else "DOWN"
            return ctx
        except Exception as exc:  # 任何异常 → 安全退化
            logger.warning("H1 regime classify failed (symbol=%s): %s", symbol, exc)
            return None

    # ── 四源投票方向（复用 M5 RegimeClassifier._compute_trend_direction 逻辑）──
    def _vote_direction(
        self,
        plus_di: float,
        minus_di: float,
        macd: float,
        macd_hist: float,
        ma_alignment: str,
        close: float,
        recent_highs: list,
        recent_lows: list,
        recent_closes: list,
    ) -> str:
        """综合判定 H1 方向（UP / DOWN / ""）。多源投票，一致更可信。

        第 5 源（B 方案）为"近 N 根 H1 收盘斜率"动量票：当近期 H1 收盘呈
        决定性单向移动时，以权重 ±2 投票，足以压过其余滞后源（MA/价格位置
        在反转初期仍指向旧方向），从根上消除 H1 方向滞后于实时行情。
        """
        votes: list = []  # +1=UP, -1=DOWN

        # 1) DI 方向（ADX 原生方向分量，最权威）
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

        # 3) MACD 方向（柱方向优先，其次线方向）
        if macd_hist > 0:
            votes.append(1)
        elif macd_hist < 0:
            votes.append(-1)
        elif macd > 0:
            votes.append(1)
        elif macd < 0:
            votes.append(-1)

        # 4) 价格位置（近 20 根 high/low 的相对位置）
        if recent_highs and recent_lows and close > 0:
            hi = max(recent_highs[-20:])
            lo = min(recent_lows[-20:])
            if hi > lo:
                pos = (close - lo) / (hi - lo)
                if pos > 0.6:
                    votes.append(1)
                elif pos < 0.4:
                    votes.append(-1)

        # 5) 【B 方案】近期收盘斜率动量票（决定性单向移动才投票，避免噪音）
        _mom = self._momentum_vote(recent_closes)
        if _mom != 0:
            votes.append(_mom)

        if not votes:
            return ""
        _net = sum(votes)
        if _net > 0:
            return "UP"
        if _net < 0:
            return "DOWN"
        return ""

    # ── 近期收盘斜率动量票（B 方案）──
    def _momentum_vote(self, recent_closes: list, n: int = H1_MOMENTUM_BARS,
                       mult: float = H1_MOMENTUM_MULT) -> int:
        """近 n 根 H1 收盘的净方向（决定性才投票，权重 ±2）。

        仅当斜率幅度 > mult × 棒间均波动（即真实趋势性移动而非噪音）才返回
        ±2，否则返回 0（不引入噪音）。这是打破"长均线 + 价格位置滞后"死锁的
        关键高权重票。
        """
        if not recent_closes or len(recent_closes) < n + 1:
            return 0
        seg = recent_closes[-(n + 1):]
        slope = seg[-1] - seg[0]
        diffs = [abs(seg[i + 1] - seg[i]) for i in range(len(seg) - 1)]
        vol = sum(diffs) / len(diffs) if diffs else 0.0
        if vol <= 0:
            return 0
        if slope < -mult * vol:
            return -2
        if slope > mult * vol:
            return 2
        return 0

    # ── 近期收盘净方向（C/E 共用：仅决定性才返回非空）──
    def _recent_trend_state(self, recent_closes: list, n: int,
                            mult: float) -> str:
        """近 n 根 H1/M5 收盘的净方向（UP/DOWN/""）。用于 C 翻转与 E 确认。

        与 ``_momentum_vote`` 同一套"决定性"判定，但返回方向字符串而非票值。
        """
        if not recent_closes or len(recent_closes) < n + 1:
            return ""
        seg = recent_closes[-(n + 1):]
        slope = seg[-1] - seg[0]
        diffs = [abs(seg[i + 1] - seg[i]) for i in range(len(seg) - 1)]
        vol = sum(diffs) / len(diffs) if diffs else 0.0
        if vol <= 0:
            return ""
        if slope < -mult * vol:
            return "DOWN"
        if slope > mult * vol:
            return "UP"
        return ""

    # ── A 方案：H1 动量强度感知 ──
    def _h1_intensity(self, recent_closes: list, n: int = H1_MOMENTUM_BARS,
                      mult: float = H1_FLIP_MULT) -> float:
        """近 n 根 H1 收盘斜率的归一化强度（|slope|/(mult×vol)，≥1 即决定性）。

        用于自适应翻转窗口：行情越急（强度越高），确认根数越少。
        """
        if not recent_closes or len(recent_closes) < n + 1:
            return 0.0
        seg = recent_closes[-(n + 1):]
        slope = abs(seg[-1] - seg[0])
        diffs = [abs(seg[i + 1] - seg[i]) for i in range(len(seg) - 1)]
        vol = sum(diffs) / len(diffs) if diffs else 0.0
        if vol <= 0:
            return 0.0
        return slope / (mult * vol)

    def _adaptive_flip_bars(self, intensity: float) -> int:
        """按 H1 动量强度返回确认根数（fast→mid→slow 兜底）。"""
        if intensity >= self._flip_intensity_fast:
            return self._flip_bars_fast
        if intensity >= self._flip_intensity_mid:
            return self._flip_bars_mid
        return self._flip_bars_slow

    def _m5_intensity(self, m5_closes: list, n: int,
                      mult: float) -> float:
        """近 n 根 M5 收盘斜率归一化强度（与 ``_h1_intensity`` 同口径）。"""
        return self._h1_intensity(m5_closes, n=n, mult=mult)

    # ── 趋势强度（ADX 归一化）──
    def _strength(self, adx: float) -> float:
        """ADX → [0,1]：0→20 映射 0→0.5，20→35 映射 0.5→1.0。"""
        if adx <= H1_ADX_STRENGTH_FLOOR:
            return 0.0
        if adx >= H1_ADX_STRENGTH_CEIL:
            return 1.0
        if adx < 20.0:
            return round(0.5 * (adx - H1_ADX_STRENGTH_FLOOR) / (20.0 - H1_ADX_STRENGTH_FLOOR), 3)
        return round(0.5 + 0.5 * (adx - 20.0) / (H1_ADX_STRENGTH_CEIL - 20.0), 3)

    # ── 4 态判定 + hysteresis ──
    def _regime(self, adx: float, direction: str, symbol: str,
                recent_closes: list, m5_dir: str = "",
                m5_int: float = 0.0) -> Optional[str]:
        """判定 H1 4 态，带 hysteresis 防止 ADX 边界抖动。

        【C 方案·A+B 自适应拐点识别】破 hysteresis 死锁：当 prev 已处于趋势态
        （BULLISH/BEARISH），按【动量强度】而非固定小时数决定翻转：
          · A 方案：H1 自身自适应窗口——行情越急（intensity 越高），确认根数
            越少（fast/mid/slow 三档），最快 1 根 H1（≈1h）即翻。
          · B 方案：M5 实时交叉确认——当 H1 仅出现"软松动"（近 2 根不再决定性
            原方向）时，若 M5 近 N 根净方向已反向且强度达标，立即提前翻转，
            不等 H1 收满（M5 比 H1 快 12 倍）。
        """
        prev = self._last_regime.get(symbol)
        trend_state = H1_BULLISH if direction == "UP" else H1_BEARISH

        # 0) 【C 方案·A+B】破死锁：prev 趋势态下，动量驱动翻转
        if prev in (H1_BULLISH, H1_BEARISH):
            # A: H1 自适应翻转窗口——行情越急、确认根数越少
            h1_intensity = self._h1_intensity(recent_closes)
            flip_bars = self._adaptive_flip_bars(h1_intensity)
            _rt = self._recent_trend_state(recent_closes, flip_bars, self._flip_mult)
            if prev == H1_BULLISH and _rt == "DOWN":
                return H1_BEARISH
            if prev == H1_BEARISH and _rt == "UP":
                return H1_BULLISH
            # B: M5 实时交叉确认——H1 软松动时由 M5 提前翻转（不等 H1 收满）
            if self._m5_confirm_enabled and m5_dir:
                prev_dir = "UP" if prev == H1_BULLISH else "DOWN"
                h1_soft = (
                    self._recent_trend_state(recent_closes, 2, self._flip_mult)
                    != prev_dir
                )
                if h1_soft and m5_int >= self._m5_confirm_intensity:
                    if prev == H1_BULLISH and m5_dir == "DOWN":
                        return H1_BEARISH
                    if prev == H1_BEARISH and m5_dir == "UP":
                        return H1_BULLISH

        # 1) 方向不明确 → 过渡（给趋势态一点惯性避免一票瞬切）
        if direction not in ("UP", "DOWN"):
            if prev in (H1_BULLISH, H1_BEARISH) and adx >= H1_REGIME_ADX_DOWN:
                return prev  # 惯性保持
            return H1_TRANSITION

        # 2) 强趋势区（ADX ≥ 24）
        if adx >= H1_REGIME_ADX_UP:
            return trend_state

        # 3) 弱趋势区（20 ≤ ADX < 24）：hysteresis
        if adx >= H1_REGIME_ADX_DOWN:
            if prev == trend_state:
                return prev  # 已处同方向趋势态 → 维持
            if adx >= H1_REGIME_ADX_MID:
                return trend_state  # ADX 接近上沿且方向明确 → 升级
            return H1_RANGE  # 否则视为区间

        # 4) ADX < 20 → 区间
        return H1_RANGE
