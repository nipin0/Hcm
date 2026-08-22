"""P0 放宽回归测试：验证 RANGE+DOWN 反向 BUY 被 P0 防火墙拦下。

A/B 对比，唯一变量是 h1.trend_direction：
  CASE A: trend_direction="DOWN"  -> 新 P0 条件触发 -> 反向被拦
  CASE B: trend_direction=""      -> 旧条件(只认 BULLISH/BEARISH)不触发 -> 放行
证明 P0 放宽是拦截的真正原因。
"""
import sys
sys.path.insert(0, "/app")

from signal_tower.scoring_engine import ScoringEngine
from signal_tower.h1_regime_classifier import H1Context
from signal_tower.indicator_calculator import IndicatorResults
from signal_tower.regime_classifier import Regime, RegimeResult


def make_h1(direction):
    # RANGE 体制但已确立 DOWN 方向(对应亏损单 1502982: RANGE + h1_dir=DOWN)
    return H1Context(regime="RANGE", trend_direction=direction,
                     trend_strength=0.5, adx=20.0)


def make_inputs():
    ind = IndicatorResults()
    # RANGE 均值回归 BUY 偏置
    ind.rsi_14 = 22.0
    ind.stoch_k = 10.0
    ind.stoch_d = 12.0
    ind.boll_upper = 1910.0
    ind.boll_lower = 1890.0
    ind.boll_middle = 1900.0
    ind.close = 1891.0          # 贴近下轨 -> BUY
    ind.pct_b = 0.05
    ind.bbw = 1.0
    ind.recent_highs = [1910.0]
    ind.recent_lows = [1890.0]
    ind.macd_histogram = 0.8
    ind.plus_di = 30.0
    ind.minus_di = 20.0
    ind.adx_14 = 20.0
    ind.ma_alignment = "bullish"
    reg = RegimeResult()
    reg.regime = Regime.RANGE
    reg.strength = 0.5
    reg.trend_direction = ""     # M5 RANGE 本身无趋势方向
    return ind, reg


def main():
    eng = ScoringEngine()

    indA, regA = make_inputs()
    resA = eng.compute_pre_score(indA, regA, h1_context=make_h1("DOWN"))

    indB, regB = make_inputs()
    resB = eng.compute_pre_score(indB, regB, h1_context=make_h1(""))

    print("CASE A (RANGE+DOWN): direction=%s pre_score=%.4f fallback_reason=%r"
          % (resA.direction, resA.pre_score, resA.fallback_reason))
    print("CASE B (RANGE+''  ): direction=%s pre_score=%.4f fallback_reason=%r"
          % (resB.direction, resB.pre_score, resB.fallback_reason))

    # 断言
    a_blocked = resA.fallback_reason.startswith("h1_reverse")
    b_open = resB.fallback_reason == ""
    same_inputs = (resA.direction != "NO_TRADE" or True)  # direction 不强制
    ok = a_blocked and b_open
    print("P0_RELAX_OK" if ok else "P0_RELAX_FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
