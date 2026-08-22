"""Unified error codes for HCM v2.

Error code format: {SERVICE}_{CATEGORY}_{CODE}

Usage:
    from shared.errors import ErrorCode, HcmError
    raise HcmError(ErrorCode.ST_AI_001, "DeepSeek API timeout")
"""

from enum import Enum
from typing import Any, Optional


class ErrorCode(str, Enum):
    """Standard error codes across all HCM v2 services."""

    # ── Gateway ──────────────────────────────────
    GW_MT5_001 = "GW_MT5_001"    # MT5 connection failed
    GW_MT5_002 = "GW_MT5_002"    # MT5 login failed
    GW_MT5_003 = "GW_MT5_003"    # MT5 heartbeat timeout
    GW_ORDER_001 = "GW_ORDER_001"  # Order placement failed
    GW_ORDER_002 = "GW_ORDER_002"  # Order cancellation failed
    GW_ORDER_003 = "GW_ORDER_003"  # Order not found
    GW_ORDER_004 = "GW_ORDER_004"  # Invalid order parameters

    # ── Collector ────────────────────────────────
    CL_KL_001 = "CL_KL_001"      # Kline collection failed
    CL_KL_002 = "CL_KL_002"      # Kline data gap detected
    CL_TK_001 = "CL_TK_001"      # Tick collection failed
    CL_TK_002 = "CL_TK_002"      # Tick data stale

    # ── Signal Tower ─────────────────────────────
    ST_AI_001 = "ST_AI_001"      # DeepSeek API timeout
    ST_AI_002 = "ST_AI_002"      # DeepSeek API error
    ST_AI_003 = "ST_AI_003"      # Circuit breaker open (bypass_ai_only)
    ST_WD_001 = "ST_WD_001"      # Watchdog heartbeat missed
    ST_WD_002 = "ST_WD_002"      # Main loop stalled
    ST_WD_003 = "ST_WD_003"      # Upstream dependency unhealthy
    ST_SIG_001 = "ST_SIG_001"    # Signal generation failed
    ST_SIG_002 = "ST_SIG_002"    # Signal publish failed (Redis Stream)
    ST_SIG_003 = "ST_SIG_003"    # Signal publish failed (PostgreSQL)
    ST_SIG_004 = "ST_SIG_004"    # Signal dead letter queued

    # ── Market Intel ─────────────────────────────
    MI_MACRO_001 = "MI_MACRO_001"  # Macro data collection failed
    MI_MACRO_002 = "MI_MACRO_002"  # Macro scoring failed
    MI_SENT_001 = "MI_SENT_001"    # Sentiment data collection failed
    MI_SENT_002 = "MI_SENT_002"    # Sentiment scoring failed
    MI_EVT_001 = "MI_EVT_001"      # Event calendar error
    MI_EVT_002 = "MI_EVT_002"      # Event warning publish failed
    MI_LIQ_001 = "MI_LIQ_001"      # Liquidity monitor error

    # ── Risk Engine ──────────────────────────────
    RK_RULE_001 = "RK_RULE_001"    # Rule evaluation error
    RK_RULE_002 = "RK_RULE_002"    # Rule chain timeout
    RK_DEC_001 = "RK_DEC_001"      # Decision output failed

    # ── Dispatcher ───────────────────────────────
    DP_ORDER_001 = "DP_ORDER_001"  # Gateway order creation failed
    DP_ORDER_002 = "DP_ORDER_002"  # Order tracking timeout
    DP_TRACK_001 = "DP_TRACK_001"  # Order status stream error

    # ── Copy Trading ─────────────────────────────
    CP_MAP_001 = "CP_MAP_001"      # Symbol mapping not found
    CP_MAP_002 = "CP_MAP_002"      # Symbol mapping refresh failed
    CP_EXEC_001 = "CP_EXEC_001"    # Copy execution failed
    CP_EXEC_002 = "CP_EXEC_002"    # Copy execution timeout (>100ms)
    CP_EXEC_003 = "CP_EXEC_003"    # Duplicate signal filtered

    # ── Web ──────────────────────────────────────
    WB_AUTH_001 = "WB_AUTH_001"    # Authentication failed
    WB_AUTH_002 = "WB_AUTH_002"    # Token expired
    WB_AUTH_003 = "WB_AUTH_003"    # Insufficient permissions
    WB_CFG_001 = "WB_CFG_001"      # Config read error
    WB_CFG_002 = "WB_CFG_002"      # Config write error
    WB_DB_001 = "WB_DB_001"        # Dashboard query error

    # ── Config ────────────────────────────────────
    CFG_LOAD_001 = "CFG_LOAD_001"  # Config load failed (all layers)
    CFG_LOAD_002 = "CFG_LOAD_002"  # Config key not found
    CFG_WRITE_001 = "CFG_WRITE_001"  # Config write failed

    # ── System (all services) ────────────────────
    SYS_DB_001 = "SYS_DB_001"      # Database connection error
    SYS_DB_002 = "SYS_DB_002"      # Database query timeout
    SYS_REDIS_001 = "SYS_REDIS_001"  # Redis connection error
    SYS_REDIS_002 = "SYS_REDIS_002"  # Redis operation timeout
    SYS_NET_001 = "SYS_NET_001"    # Network error
    SYS_NET_002 = "SYS_NET_002"    # gRPC connection error


class HcmError(Exception):
    """Base exception for HCM v2 with error code."""

    def __init__(self, code: ErrorCode, message: str = "", details: Optional[dict] = None):
        self.code = code
        self.message = message or code.value
        self.details = details or {}
        super().__init__(f"[{self.code.value}] {self.message}")

    def to_dict(self) -> dict:
        """Serialize error to dict for API responses."""
        return {
            "code": self.code.value,
            "message": self.message,
            "details": self.details,
        }


class ConfigNotFoundError(HcmError):
    """Raised when a config key is not found in any layer."""

    def __init__(self, key: str):
        super().__init__(
            ErrorCode.CFG_LOAD_002,
            f"Config key not found: {key}",
            {"key": key},
        )


class DatabaseError(HcmError):
    """Raised for database connection or query errors."""

    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(ErrorCode.SYS_DB_001, message, details)


class RedisError(HcmError):
    """Raised for Redis connection or operation errors."""

    def __init__(self, message: str, details: Optional[dict] = None):
        super().__init__(ErrorCode.SYS_REDIS_001, message, details)
