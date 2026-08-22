"""hcm-signal-tower core modules.

Signal Tower is the core signal production engine:
- Scheduler: bar_close trigger → signal production pipeline
- Regime Classifier: five-level market regime detection (PRE_TREND/TREND/TREND_FADE/RANGE/NEUTRAL)
- Scoring Engine: weighted indicator scoring with regime-aware weight schemes
- Range Bonus: oscillation zone position-based score enhancement
- Indicator Calculator: technical indicator computation (RSI/MACD/ADX/Boll/Stoch/MA)
- Watchdog: four-dimensional health monitoring
- Signal Publisher: Redis Stream XADD + PostgreSQL INSERT dual-write
"""

__all__ = [
    "scheduler",
    "scoring_engine",
    "regime_classifier",
    "range_bonus",
    "indicator_calculator",
    "watchdog",
    "signal_publisher",
]
