"""hcm-dispatcher core modules.

Dispatcher is the order dispatch layer in the HCM v2 event-driven pipeline:
- Stream Consumer: XREADGROUP on signal:risk_passed via Redis consumer group
- Gateway Client: gRPC client connecting to hcm-gateway for PlaceOrder/CancelOrder
- Order Tracker: order lifecycle tracking with timeout handling and execution records
"""

__all__ = [
    "stream_consumer",
    "gateway_client",
    "order_tracker",
]
