"""hcm-web core modules.

Web is the management backend + config center + dashboard:
- RESTful CRUD APIs for system configuration
- JWT authentication + RBAC
- WebSocket real-time push (signals, positions, events)
- Dashboard aggregation queries
- System health aggregation across all services
"""

__all__ = [
    "api",
    "ws",
]
