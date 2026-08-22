"""hcm-copy-trading core modules.

Copy Trading Engine is the signal replication layer in HCM v2:
- Stream Consumer: XREADGROUP on signal:risk_passed via Redis consumer group
- Symbol Mapper: in-memory O(1) symbol mapping with Redis PUB/SUB refresh
- Lot Calculator: five-mode lot size calculation with local cache
- Order Executor: gRPC order placement with <100ms latency target
"""

__all__ = [
    "stream_consumer",
    "symbol_mapper",
    "lot_calculator",
    "order_executor",
]
