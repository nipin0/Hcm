"""hcm-web API modules.

Provides RESTful API endpoints:
- auth: JWT login/logout/token refresh + RBAC
- config: Configuration CRUD with category filtering
- symbols: Symbol management and activation
- signals: Signal query with filtering and pagination
- positions: Current and historical position queries
- dashboard: Aggregated stats and analytics
- health_api: System-wide health aggregation
- copy: Copy trading relationships + symbol mappings + trade logs
- risk: Risk control config + symbol limits + intercept logs
- signal_tower: Cooldown config + dual-mode switching + symbol tower config
- dispatch: Order dispatch configuration
- close: Position close configuration
- datasource: Data source configuration
- engine: Inference engine rules CRUD
- system: User management + MT5/DeepSeek/Network/Notifications/Cache
- cosource: Co-source signal enhancement config CRUD (P1a/P1b)
"""

__all__ = [
    "auth",
    "config",
    "symbols",
    "signals",
    "positions",
    "dashboard",
    "health_api",
    "copy",
    "risk",
    "signal_tower",
    "dispatch",
    "close",
    "datasource",
    "engine",
    "system",
    "cosource",
]
