"""Watchdog Manager — Four-Dimensional Health Monitoring.

Per PRD §6.4, the watchdog monitors:
  L1: Heartbeat — main loop alive check (every 30s)
  L2: Submodule Timeout — Kline/Indicator/DeepSeek/Publish step timeouts
  L3: Upstream Reachability — PG/Redis/DeepSeek/Kline health
  L4: Service Health — exposes /health endpoint

Raises alerts via Redis PUB/SUB and logging on failures.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Default Config ─────────────────────────────

DEFAULT_HEARTBEAT_INTERVAL = 30
DEFAULT_STALL_THRESHOLD = 300  # 5 minutes
DEFAULT_LOOP_TIMEOUT = 600     # 10 minutes
DEFAULT_UPSTREAM_CHECK_INTERVAL = 30
DEFAULT_UPSTREAM_FAILURE_THRESHOLD = 3
DEFAULT_DEEPSEEK_DEGRADATION_THRESHOLD = 5
DEFAULT_KLINE_STALE_THRESHOLD = 600  # 10 minutes


class WatchdogState(str, Enum):
    """Watchdog health states."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class WatchdogStatus:
    """Aggregated watchdog status report."""
    # L1: Heartbeat
    heartbeat_ok: bool = True
    last_heartbeat: float = field(default_factory=time.time)
    heartbeat_missed: int = 0

    # L2: Submodule timeouts
    submodule_timeouts: dict[str, int] = field(default_factory=lambda: {
        "kline": 0,
        "indicator": 0,
        "deepseek": 0,
        "publish": 0,
    })
    step_latencies: dict[str, float] = field(default_factory=dict)

    # L3: Upstream health
    upstream_status: dict[str, str] = field(default_factory=lambda: {
        "postgresql": "unknown",
        "redis": "unknown",
        "deepseek": "unknown",
        "kline_data": "unknown",
    })
    upstream_failure_counts: dict[str, int] = field(default_factory=lambda: {
        "postgresql": 0,
        "redis": 0,
        "deepseek": 0,
        "kline_data": 0,
    })

    # L4: Overall
    overall_state: WatchdogState = WatchdogState.HEALTHY
    stall_duration: float = 0.0
    alerts: list[str] = field(default_factory=list)


class WatchdogManager:
    """Four-dimensional watchdog for signal-tower health monitoring.

    Monitors heartbeat, submodule timeouts, upstream dependencies,
    and overall service health. Publishes alerts via Redis PUB/SUB.

    Example:
        watchdog = WatchdogManager(redis_client=redis, config_provider=config)
        await watchdog.start()

        # Signal producer calls beat() on each loop iteration
        await watchdog.beat()

        # Report step timing
        await watchdog.report_step("indicator", 0.25)
    """

    def __init__(
        self,
        redis_client: Any = None,
        config_provider: Any = None,
        heartbeat_interval: int = DEFAULT_HEARTBEAT_INTERVAL,
        stall_threshold: int = DEFAULT_STALL_THRESHOLD,
        loop_timeout: int = DEFAULT_LOOP_TIMEOUT,
        upstream_check_interval: int = DEFAULT_UPSTREAM_CHECK_INTERVAL,
        upstream_failure_threshold: int = DEFAULT_UPSTREAM_FAILURE_THRESHOLD,
        deepseek_degradation_threshold: int = DEFAULT_DEEPSEEK_DEGRADATION_THRESHOLD,
        kline_stale_threshold: int = DEFAULT_KLINE_STALE_THRESHOLD,
    ):
        """Initialize WatchdogManager.

        Args:
            redis_client: RedisClient for PUB/SUB alerts.
            config_provider: ConfigProviderV3 for runtime configuration.
            heartbeat_interval: Heartbeat check interval in seconds.
            stall_threshold: Seconds before declaring main loop stalled.
            loop_timeout: Max allowed single loop duration.
            upstream_check_interval: Interval for upstream health checks.
            upstream_failure_threshold: Consecutive failures before alerting.
            deepseek_degradation_threshold: DeepSeek failures before degradation.
            kline_stale_threshold: Seconds before K-line considered stale.
        """
        self._redis = redis_client
        self._config = config_provider
        self._heartbeat_interval = heartbeat_interval
        self._stall_threshold = stall_threshold
        self._loop_timeout = loop_timeout
        self._upstream_check_interval = upstream_check_interval
        self._upstream_failure_threshold = upstream_failure_threshold
        self._deepseek_degradation_threshold = deepseek_degradation_threshold
        self._kline_stale_threshold = kline_stale_threshold

        self._status = WatchdogStatus()
        self._start_time: float = time.time()
        self._running = False
        self._loop_start_time: float = 0.0
        self._monitor_task: Optional[asyncio.Task] = None

        # Callbacks for upstream checks
        self._upstream_checkers: dict[str, Any] = {}

    # ── Lifecycle ───────────────────────────────

    async def start(self) -> None:
        """Start watchdog monitoring."""
        self._running = True
        self._start_time = time.time()
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        logger.info(
            "Watchdog started: heartbeat=%ds, stall=%ds, upstream_check=%ds",
            self._heartbeat_interval, self._stall_threshold, self._upstream_check_interval,
        )

    async def stop(self) -> None:
        """Stop watchdog monitoring."""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        logger.info("Watchdog stopped")

    # ── L1: Heartbeat ───────────────────────────

    async def beat(self) -> None:
        """Register a heartbeat from the main loop.

        Called at the end of each signal production cycle.
        """
        self._status.last_heartbeat = time.time()
        self._status.heartbeat_ok = True
        self._status.heartbeat_missed = 0

        # Reset stall detection
        self._status.stall_duration = 0.0

    def mark_loop_start(self) -> None:
        """Mark the start of a signal production cycle."""
        self._loop_start_time = time.time()

    def mark_loop_end(self) -> None:
        """Mark the end of a signal production cycle."""
        loop_duration = time.time() - self._loop_start_time
        if loop_duration > self._loop_timeout:
            logger.warning(
                "Loop timeout: duration=%.1fs > threshold=%ds",
                loop_duration, self._loop_timeout,
            )
            self._status.alerts.append(f"Loop timeout: {loop_duration:.1f}s")

    # ── L2: Submodule Timeout ──────────────────

    async def report_step(
        self, step_name: str, duration_seconds: float, timeout_seconds: float = 0.0
    ) -> None:
        """Report timing for a signal production step.

        Args:
            step_name: Step identifier (kline, indicator, deepseek, publish).
            duration_seconds: Actual duration.
            timeout_seconds: Timeout threshold (0 = no timeout check).
        """
        self._status.step_latencies[step_name] = duration_seconds

        if timeout_seconds > 0 and duration_seconds > timeout_seconds:
            self._status.submodule_timeouts[step_name] = (
                self._status.submodule_timeouts.get(step_name, 0) + 1
            )
            logger.warning(
                "Submodule timeout: step=%s, duration=%.1fs > timeout=%.1fs (count=%d)",
                step_name, duration_seconds, timeout_seconds,
                self._status.submodule_timeouts[step_name],
            )
            self._status.alerts.append(
                f"Submodule timeout: {step_name} ({duration_seconds:.1f}s)"
            )

    # ── L3: Upstream Reachability ──────────────

    def register_upstream_checker(self, name: str, checker: Any) -> None:
        """Register an upstream health check function.

        Args:
            name: Upstream name (e.g., "postgresql").
            checker: Async callable returning dict with "status" key.
        """
        self._upstream_checkers[name] = checker
        logger.debug("Upstream checker registered: %s", name)

    async def check_upstream(self, name: str) -> dict:
        """Run a specific upstream health check.

        Args:
            name: Upstream name.

        Returns:
            Status dict.
        """
        checker = self._upstream_checkers.get(name)
        if checker is None:
            return {"status": "unknown", "error": "no checker registered"}

        try:
            result = await checker()
            status = result.get("status", "unknown")

            if status == "healthy":
                self._status.upstream_status[name] = "healthy"
                self._status.upstream_failure_counts[name] = 0
            else:
                self._status.upstream_status[name] = "unhealthy"
                self._status.upstream_failure_counts[name] += 1
                if self._status.upstream_failure_counts[name] >= self._upstream_failure_threshold:
                    await self._alert(
                        f"Upstream {name} unhealthy: {result.get('error', 'unknown')}"
                    )

            return result

        except Exception as exc:
            self._status.upstream_status[name] = "unhealthy"
            self._status.upstream_failure_counts[name] += 1
            if self._status.upstream_failure_counts[name] >= self._upstream_failure_threshold:
                await self._alert(f"Upstream {name} check failed: {exc}")
            return {"status": "unhealthy", "error": str(exc)}

    # ── L4: Overall Health ─────────────────────

    async def get_health(self) -> dict:
        """Get comprehensive health status.

        Returns:
            Dict with L1-L4 health status.
        """
        # Determine overall state
        if not self._status.heartbeat_ok or self._status.stall_duration > self._stall_threshold:
            self._status.overall_state = WatchdogState.UNHEALTHY
        elif any(
            v >= self._upstream_failure_threshold
            for v in self._status.upstream_failure_counts.values()
        ):
            self._status.overall_state = WatchdogState.DEGRADED
        else:
            self._status.overall_state = WatchdogState.HEALTHY

        uptime = time.time() - self._start_time

        return {
            "status": self._status.overall_state.value,
            "uptime_seconds": round(uptime, 1),
            "l1_heartbeat": {
                "ok": self._status.heartbeat_ok,
                "missed": self._status.heartbeat_missed,
                "seconds_since_last": round(time.time() - self._status.last_heartbeat, 1),
            },
            "l2_submodules": {
                "timeouts": self._status.submodule_timeouts,
                "recent_latencies": self._status.step_latencies,
            },
            "l3_upstream": self._status.upstream_status,
            "l4_alerts": self._status.alerts[-10:],  # Last 10 alerts
        }

    # ── Monitoring Loop ─────────────────────────

    async def _monitor_loop(self) -> None:
        """Background monitoring loop."""
        logger.info("Watchdog monitor loop started")
        try:
            while self._running:
                await asyncio.sleep(self._heartbeat_interval)

                # Check heartbeat staleness
                elapsed = time.time() - self._status.last_heartbeat
                if elapsed > self._stall_threshold:
                    self._status.stall_duration = elapsed
                    self._status.heartbeat_ok = False
                    self._status.heartbeat_missed += 1
                    await self._alert(
                        f"Main loop stalled: {elapsed:.0f}s since last heartbeat "
                        f"(threshold={self._stall_threshold}s)"
                    )

                # Periodic upstream checks
                if int(elapsed) % self._upstream_check_interval < self._heartbeat_interval:
                    for name in self._upstream_checkers:
                        await self.check_upstream(name)

        except asyncio.CancelledError:
            logger.info("Watchdog monitor loop stopped")

    # ── Alerting ────────────────────────────────

    async def _alert(self, message: str) -> None:
        """Send an alert via Redis PUB/SUB and logging.

        Args:
            message: Alert message.
        """
        self._status.alerts.append(f"{time.strftime('%H:%M:%S')} {message}")

        # Log the alert
        logger.warning("WATCHDOG ALERT: %s", message)

        # Publish to Redis if available
        if self._redis is not None and self._redis.is_initialized:
            try:
                await self._redis.publish(
                    "hcm:event:circuit_breaker",
                    json.dumps({
                        "event": "watchdog_alert",
                        "message": message,
                        "timestamp": time.time(),
                    }),
                )
            except Exception as exc:
                logger.error("Watchdog alert publish failed: %s", exc)

    # ── Config ──────────────────────────────────

    async def load_config(self) -> None:
        """Load watchdog parameters from config_provider."""
        if self._config is None:
            return

        try:
            self._heartbeat_interval = await self._config.get_int(
                "watchdog_interval_sec", DEFAULT_HEARTBEAT_INTERVAL
            )
            self._stall_threshold = await self._config.get_int(
                "watchdog_stall_threshold_sec", DEFAULT_STALL_THRESHOLD
            )
            self._loop_timeout = await self._config.get_int(
                "watchdog_loop_timeout_sec", DEFAULT_LOOP_TIMEOUT
            )
            self._upstream_failure_threshold = await self._config.get_int(
                "upstream_failure_threshold", DEFAULT_UPSTREAM_FAILURE_THRESHOLD
            )
            self._deepseek_degradation_threshold = await self._config.get_int(
                "deepseek_degradation_threshold", DEFAULT_DEEPSEEK_DEGRADATION_THRESHOLD
            )
            self._kline_stale_threshold = await self._config.get_int(
                "kline_stale_threshold_sec", DEFAULT_KLINE_STALE_THRESHOLD
            )
            logger.info("Watchdog config loaded")
        except Exception as exc:
            logger.warning("Watchdog config load failed: %s", exc)

    @property
    def status(self) -> WatchdogStatus:
        """Current watchdog status."""
        return self._status
