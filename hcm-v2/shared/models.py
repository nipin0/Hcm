"""Shared Pydantic data models for HCM v2.

This module defines common data structures used across services.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────
# API Response Models
# ──────────────────────────────────────────────

class ApiResponse(BaseModel):
    """Standard API response wrapper."""
    code: int = 0
    data: Any = None
    message: str = "ok"


class PaginatedResponse(BaseModel):
    """Standard paginated list response."""
    items: list = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 50


class HealthStatus(BaseModel):
    """Standard /health endpoint response."""
    status: str = "healthy"      # healthy / degraded / unhealthy
    service: str = ""
    version: str = "2.0.0"
    uptime_seconds: float = 0.0
    checks: dict = Field(default_factory=dict)


# ──────────────────────────────────────────────
# Config Models
# ──────────────────────────────────────────────

class ConfigEntry(BaseModel):
    """A single configuration entry."""
    config_key: str
    category: str
    subcategory: Optional[str] = None
    default_value: str
    current_value: Optional[str] = None
    value_type: str = "string"
    label: Optional[str] = None
    description: Optional[str] = None
    ui_control: str = "text"
    ui_options: Optional[dict] = None
    ui_order: int = 0
    scope: str = "global"
    is_sensitive: bool = False


class ConfigOverride(BaseModel):
    """Account-level config override."""
    account_id: int
    config_key: str
    override_value: str


# ──────────────────────────────────────────────
# Signal & Trading Models
# ──────────────────────────────────────────────

class SignalDirection(str):
    """Signal direction constants."""
    BUY = "BUY"
    SELL = "SELL"
    NO_TRADE = "NO_TRADE"


class RegimeType(str):
    """Market regime types (v1.3 five-level model)."""
    PRE_TREND = "PRE_TREND"
    TREND = "TREND"
    TREND_FADE = "TREND_FADE"
    RANGE = "RANGE"
    NEUTRAL = "NEUTRAL"


class SignalStreamMessage(BaseModel):
    """Redis Stream signal message format."""
    event: str = "signal_created"
    timestamp: str = ""
    signal_id: int = 0
    task_id: int = 0
    account_id: int = 0
    symbol: str = ""
    time_frame: str = "M5"
    direction: str = ""
    entry_price: float = 0.0
    sl_price: float = 0.0
    tp1: float = 0.0
    tp2: float = 0.0
    lot: float = 0.0
    confidence: float = 0.0
    signal_mode: str = "indicator_scoring"
    indicator_values: dict = Field(default_factory=dict)
    macro_snapshot_id: Optional[int] = None
    sentiment_snapshot_id: Optional[int] = None
    fallback_reason: Optional[str] = None
    regime: Optional[str] = None
    pre_score: Optional[float] = None
    weight_scheme: Optional[str] = None
    position_in_range: Optional[float] = None


class RiskResultMessage(BaseModel):
    """Risk check result for Redis Stream."""
    event: str = "risk_check_passed"
    signal_id: int = 0
    passed: bool = False
    rejected_rules: list = Field(default_factory=list)


class DispatchedMessage(BaseModel):
    """Order placed confirmation for Redis Stream."""
    event: str = "order_placed"
    signal_id: int = 0
    mt5_ticket: int = 0
    latency_ms: int = 0


class DeadLetterMessage(BaseModel):
    """Dead letter queue message for Redis Stream."""
    original_signal_id: int = 0
    failed_consumer: str = ""
    error: str = ""
    retry_count: int = 0
    timestamp: str = ""


# ──────────────────────────────────────────────
# Market Data Models
# ──────────────────────────────────────────────

class OHLCV(BaseModel):
    """Single OHLCV candle."""
    symbol: str = ""
    time_frame: str = "M5"
    open_time: datetime = Field(default_factory=datetime.utcnow)
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    tick_volume: int = 0


class Tick(BaseModel):
    """Single tick data point."""
    symbol: str = ""
    bid: float = 0.0
    ask: float = 0.0
    spread: float = 0.0
    volume: int = 0
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class MacroSnapshot(BaseModel):
    """Macro environment snapshot."""
    id: Optional[int] = None
    category: str = ""             # metals / crypto / forex
    macro_risk_score: int = 0
    macro_bias: str = ""           # bullish / bearish / neutral
    ai_summary: str = ""
    category_data: dict = Field(default_factory=dict)


class SentimentSnapshot(BaseModel):
    """Market sentiment snapshot."""
    id: Optional[int] = None
    category: str = ""
    sentiment_risk_score: int = 0
    sentiment_bias: str = ""
    ai_summary: str = ""
    category_data: dict = Field(default_factory=dict)


# ──────────────────────────────────────────────
# Account & Broker Models
# ──────────────────────────────────────────────

class AccountInfo(BaseModel):
    """Broker account summary."""
    account_id: int = 0
    account_name: str = ""
    account_number: int = 0
    broker_name: str = ""
    account_type: str = "master"
    base_currency: str = "USD"
    leverage: int = 100
    is_active: bool = True
    last_balance: Optional[float] = None
    last_equity: Optional[float] = None


class SymbolMeta(BaseModel):
    """Symbol metadata."""
    symbol: str = ""
    category: str = ""             # metals / crypto / forex
    display_name: str = ""
    base_currency: str = "USD"
    quote_currency: str = ""
    lot_step: float = 0.01
    min_lot: float = 0.01
    max_lot: float = 5.0
    pip_value: Optional[float] = None
    is_active: bool = True
    phase: int = 1


# ──────────────────────────────────────────────
# Gap Implementation — Shared Response Models
# ──────────────────────────────────────────────

class CopyRelationshipResponse(BaseModel):
    """Copy trading relationship response (all fields)."""
    rel_id: int = 0
    master_account_id: int = 0
    follower_account_id: int = 0
    lot_mode: str = "multiplier"
    lot_multiplier: float = 1.0
    min_lot: float = 0.01
    max_lot: float = 5.0
    max_positions: int = 10
    direction_mode: str = "FORWARD"
    copy_sl: bool = True
    copy_tp: bool = True
    max_daily_loss: float = 0.0
    max_consecutive_losses: int = 3
    sync_mode: str = "pubsub"
    status: str = "stopped"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SymbolMappingResponse(BaseModel):
    """Cross-broker symbol mapping response."""
    mapping_id: int = 0
    master_broker: str = ""
    master_symbol: str = ""
    follower_broker: str = ""
    follower_symbol: str = ""
    match_mode: str = "exact"
    match_priority: int = 0
    is_active: bool = True
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SymbolLimitItem(BaseModel):
    """Per-symbol position/trade limit configuration."""
    symbol: str = ""
    max_positions: int = 0
    max_daily_trades: int = 0


class CooldownConfigResponse(BaseModel):
    """Five-level market regime cooldown configuration response."""
    pretrend_cooldown_seconds: int = 180
    trend_cooldown_seconds: int = 120
    trend_same_dir_bypass_score: float = 0.60
    trend_same_dir_mid_cooldown: int = 60
    trend_reverse_cooldown_seconds: int = 300
    fade_cooldown_seconds: int = 300
    range_boundary_cooldown_seconds: int = 0
    neutral_cooldown_seconds: int = 300
