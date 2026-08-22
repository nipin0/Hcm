"""Prometheus metrics instrumentation for HCM v2 services.

Provides standard metrics for all services:
- Counter: total requests, errors, signals produced
- Histogram: request latency, signal processing duration
- Gauge: active connections, queue depth

Each service exposes metrics at GET /metrics (Prometheus scrape target).
"""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client import REGISTRY as DEFAULT_REGISTRY

# ── Registry ───────────────────────────────────
_shared_registry: CollectorRegistry = DEFAULT_REGISTRY


def get_registry() -> CollectorRegistry:
    """Get the shared Prometheus registry."""
    return _shared_registry


# ── Standard Counters ──────────────────────────

signals_produced = Counter(
    "hcm_signals_produced_total",
    "Total number of trading signals produced",
    ["service", "symbol", "direction"],
    registry=_shared_registry,
)

signals_published = Counter(
    "hcm_signals_published_total",
    "Total number of signals published to Redis Stream",
    ["service", "stream"],
    registry=_shared_registry,
)

signals_bypassed = Counter(
    "hcm_signals_bypassed_total",
    "Signals that bypassed AI (bypass_ai)",
    ["service", "reason"],
    registry=_shared_registry,
)

orders_placed = Counter(
    "hcm_orders_placed_total",
    "Total orders placed via Gateway",
    ["symbol", "direction"],
    registry=_shared_registry,
)

errors_total = Counter(
    "hcm_errors_total",
    "Total errors by service and error code",
    ["service", "error_code"],
    registry=_shared_registry,
)

copy_executions = Counter(
    "hcm_copy_executions_total",
    "Total copy trading executions",
    ["status", "symbol"],
    registry=_shared_registry,
)

deepseek_calls = Counter(
    "hcm_deepseek_calls_total",
    "Total DeepSeek API calls",
    ["service", "status"],
    registry=_shared_registry,
)

circuit_breaker_trips = Counter(
    "hcm_circuit_breaker_trips_total",
    "Total circuit breaker activations",
    ["service"],
    registry=_shared_registry,
)

# ── Standard Histograms ────────────────────────

signal_production_duration = Histogram(
    "hcm_signal_production_duration_seconds",
    "Signal production duration by step",
    ["service", "step"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 15.0],
    registry=_shared_registry,
)

copy_latency = Histogram(
    "hcm_copy_latency_seconds",
    "End-to-end copy trading latency",
    ["symbol"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 1.0],
    registry=_shared_registry,
)

http_request_duration = Histogram(
    "hcm_http_request_duration_seconds",
    "HTTP request duration",
    ["service", "method", "endpoint"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
    registry=_shared_registry,
)

db_query_duration = Histogram(
    "hcm_db_query_duration_seconds",
    "Database query duration",
    ["service", "operation"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5],
    registry=_shared_registry,
)

redis_operation_duration = Histogram(
    "hcm_redis_operation_duration_seconds",
    "Redis operation duration",
    ["service", "operation"],
    buckets=[0.0001, 0.0005, 0.001, 0.005, 0.01, 0.025, 0.05, 0.1],
    registry=_shared_registry,
)

# ── Standard Gauges ────────────────────────────

service_uptime = Gauge(
    "hcm_service_uptime_seconds",
    "Service uptime in seconds",
    ["service"],
    registry=_shared_registry,
)

stream_pending = Gauge(
    "hcm_stream_pending_total",
    "Pending messages in Stream consumer group",
    ["stream", "group"],
    registry=_shared_registry,
)

active_positions = Gauge(
    "hcm_active_positions",
    "Currently active trading positions",
    ["account_id", "symbol"],
    registry=_shared_registry,
)

circuit_breaker_state = Gauge(
    "hcm_circuit_breaker_state",
    "Circuit breaker state (0=closed, 1=open)",
    ["service"],
    registry=_shared_registry,
)

# ── Metrics Endpoint ───────────────────────────

def metrics_response() -> bytes:
    """Generate metrics response for /metrics endpoint.

    Returns:
        Prometheus text format bytes.
    """
    return generate_latest(_shared_registry)
