"""hcm-gateway core modules.

Gateway provides the MT5 bridge layer for HCM v2:
- gRPC server: order placement, cancellation, price streaming, account info
- TCP bridge: MT5 Terminal direct connection
- WebSocket server: real-time quote push to Dashboard
- Order manager: order caching and state machine
"""

__all__ = [
    "grpc_server",
    "tcp_bridge",
    "ws_server",
    "order_manager",
]
