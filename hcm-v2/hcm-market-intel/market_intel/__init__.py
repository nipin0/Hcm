"""hcm-market-intel core modules.

Market Intel is the four-in-one market intelligence service:
- Macro Collector: macro data collection (FRED/CME/BLS/Investing)
- Sentiment Collector: market sentiment collection (COT/ETF/VIX/Fear&Greed)
- Event Calendar: economic calendar with trading halt triggers
- Liquidity Analyzer: real-time spread/depth monitoring
- AI Scorer: DeepSeek-based factor scoring engine
"""

__all__ = [
    "ai_scorer",
    "macro_collector",
    "sentiment_collector",
    "event_calendar",
    "liquidity_analyzer",
]
