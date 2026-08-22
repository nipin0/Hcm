"""hcm-collector core modules.

Collector gathers market data from hcm-gateway:
- Kline collector: multi-symbol, multi-timeframe OHLCV from gRPC StreamPrices
- Tick collector: real-time bid/ask/spread collection
"""

__all__ = [
    "kline_collector",
    "tick_collector",
]
