"""Event Calendar — Economic calendar with trading halt triggers.

Manages the economic event calendar, detects upcoming events,
triggers trading halts (flat_before/flat_after), scales lots/confidence,
and publishes event warnings via Redis PUB/SUB (event:warning).

Integrates with the AI scorer for event risk scoring.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Default Config ─────────────────────────────

DEFAULT_CHECK_INTERVAL = 60  # seconds
DEFAULT_WARNING_ADVANCE_MIN = 30  # minutes before event
DEFAULT_CATEGORIES = ["all", "metals", "crypto", "forex"]

# Redis keys and channels
REDIS_EVENT_ACTIVE = "event:active"
REDIS_EVENT_CHANNEL = "event:warning"
REDIS_CIRCUIT_BREAKER_CHANNEL = "hcm:event:circuit_breaker"


@dataclass
class EventInfo:
    """Economic calendar event information.

    Attributes:
        event_id: Database event ID.
        event_name: Human-readable event name.
        category: Affected category (all/metals/crypto/forex).
        event_date: UTC event datetime.
        importance: 1=MEDIUM, 2=HIGH, 3=CRITICAL.
        flat_before_min: Minutes to halt trading before event.
        flat_after_min: Minutes to halt trading after event.
        lot_scale: Lot size multiplier during event (0=halt, 0.5=half, 1.0=normal).
        confidence_discount: Confidence discount (0.0-1.0).
        is_active: Whether event is active.
    """

    event_id: int = 0
    event_name: str = ""
    category: str = "all"
    event_date: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    importance: int = 1
    flat_before_min: int = 0
    flat_after_min: int = 0
    lot_scale: float = 1.0
    confidence_discount: float = 0.0
    is_active: bool = True


@dataclass
class EventState:
    """Current state of an event relative to now.

    Attributes:
        event: The EventInfo.
        status: "upcoming", "active" (in flat window), "passed", "cancelled".
        minutes_to_start: Minutes until event starts (negative = passed).
        in_flat_before: Whether currently in pre-event flat window.
        in_flat_after: Whether currently in post-event flat window.
        trading_halted: Whether trading should be halted.
    """

    event: EventInfo = field(default_factory=EventInfo)
    status: str = "upcoming"
    minutes_to_start: float = 0.0
    in_flat_before: bool = False
    in_flat_after: bool = False
    trading_halted: bool = False


class EventCalendar:
    """Economic event calendar with trading halt management.

    Loads events from PostgreSQL event_calendar table, monitors
    event windows, triggers trading halts, and publishes warnings
    via Redis PUB/SUB.

    Example:
        calendar = EventCalendar(db_pool, redis_client, ai_scorer, config_provider)
        await calendar.load_events()
        active_events = await calendar.check_active(category="metals")
    """

    def __init__(
        self,
        db_pool: Any = None,
        redis_client: Any = None,
        ai_scorer: Any = None,
        config_provider: Any = None,
        check_interval: int = DEFAULT_CHECK_INTERVAL,
        warning_advance_min: int = DEFAULT_WARNING_ADVANCE_MIN,
    ):
        """Initialize EventCalendar.

        Args:
            db_pool: DatabasePool for PG reads.
            redis_client: RedisClient for PUB/SUB and cache.
            ai_scorer: AiScorer for event risk scoring.
            config_provider: ConfigProviderV3 for runtime config.
            check_interval: Event check interval in seconds.
            warning_advance_min: Minutes before event to publish warning.
        """
        self._db = db_pool
        self._redis = redis_client
        self._ai_scorer = ai_scorer
        self._config = config_provider
        self._check_interval = check_interval
        self._warning_advance_min = warning_advance_min

        # Loaded events (keyed by event_id)
        self._events: dict[int, EventInfo] = {}
        self._last_load: float = 0.0

        # Active event states for each category
        self._category_states: dict[str, list[EventState]] = {
            cat: [] for cat in DEFAULT_CATEGORIES
        }

        # Stats
        self._stats: dict[str, int] = {
            "events_loaded": 0,
            "warnings_published": 0,
            "halts_triggered": 0,
            "checks": 0,
            "errors": 0,
        }

    # ── Event Loading ───────────────────────────

    async def load_events(self, force: bool = False) -> int:
        """Load active events from PostgreSQL.

        Args:
            force: If True, reload even if recently loaded.

        Returns:
            Number of events loaded.
        """
        if not force and (time.time() - self._last_load) < 300:
            logger.debug("Events recently loaded (%ds ago), skipping", int(time.time() - self._last_load))
            return len(self._events)

        if self._db is None or not self._db.is_initialized:
            logger.warning("Database not available — cannot load events")
            return 0

        try:
            rows = await self._db.fetch(
                """SELECT event_id, event_name, category, event_date, importance,
                          flat_before_min, flat_after_min, lot_scale, confidence_discount, is_active
                   FROM hcm_market.event_calendar
                   WHERE is_active = true
                     AND event_date >= $1
                   ORDER BY event_date ASC""",
                datetime.now(timezone.utc) - timedelta(days=1),
            )

            self._events.clear()
            for row in rows:
                self._events[row["event_id"]] = EventInfo(
                    event_id=row["event_id"],
                    event_name=row["event_name"],
                    category=row["category"],
                    event_date=row["event_date"].replace(tzinfo=timezone.utc)
                    if row["event_date"].tzinfo is None
                    else row["event_date"],
                    importance=row["importance"],
                    flat_before_min=row["flat_before_min"] or 0,
                    flat_after_min=row["flat_after_min"] or 0,
                    lot_scale=float(row["lot_scale"] or 1.0),
                    confidence_discount=float(row["confidence_discount"] or 0.0),
                    is_active=row["is_active"],
                )

            self._last_load = time.time()
            self._stats["events_loaded"] = len(self._events)
            logger.info("Loaded %d active events from calendar", len(self._events))
            return len(self._events)

        except Exception as exc:
            logger.error("Failed to load events: %s", exc)
            self._stats["errors"] += 1
            return 0

    # ── Event Checking ──────────────────────────

    async def check_all(self) -> dict[str, list[EventState]]:
        """Check all categories for active/upcoming events.

        Returns:
            Dict mapping category → list of EventState.
        """
        self._stats["checks"] += 1
        now = datetime.now(timezone.utc)

        # Ensure events are loaded
        if not self._events:
            await self.load_events()

        results: dict[str, list[EventState]] = {}

        for category in DEFAULT_CATEGORIES:
            states = self._check_category(category, now)
            self._category_states[category] = states

            # Check for trading halt triggers
            for state in states:
                if state.trading_halted and state.status == "active":
                    await self._trigger_halt(state)
                elif state.status == "upcoming" and state.minutes_to_start <= self._warning_advance_min:
                    await self._publish_warning(state)

            results[category] = states

        # Update Redis event:active cache
        await self._update_redis_active()

        return results

    async def check_active(self, category: str = "all") -> list[EventState]:
        """Check active events for a specific category.

        Args:
            category: Asset category (or "all" for global events).

        Returns:
            List of active EventState.
        """
        now = datetime.now(timezone.utc)

        if not self._events:
            await self.load_events()

        states = self._check_category(category, now)
        return [s for s in states if s.status == "active"]

    def _check_category(self, category: str, now: datetime) -> list[EventState]:
        """Check event states for a category at a given time.

        Args:
            category: Asset category.
            now: Current UTC datetime.

        Returns:
            List of EventState for this category.
        """
        states: list[EventState] = []

        for event in self._events.values():
            # Filter by category: "all" matches everything
            if event.category not in (category, "all"):
                continue

            state = self._compute_state(event, now)
            states.append(state)

        # Sort: active first, then upcoming by start time
        states.sort(key=lambda s: (
            0 if s.status == "active" else (1 if s.status == "upcoming" else 2),
            abs(s.minutes_to_start),
        ))

        return states

    @staticmethod
    def _compute_state(event: EventInfo, now: datetime) -> EventState:
        """Compute the current state of an event.

        Args:
            event: EventInfo.
            now: Current UTC datetime.

        Returns:
            EventState with computed status.
        """
        total_seconds = (event.event_date - now).total_seconds()
        minutes_to_start = total_seconds / 60.0

        state = EventState(event=event, minutes_to_start=round(minutes_to_start, 1))

        # Determine if in flat window
        if minutes_to_start < -(event.flat_after_min):
            # Event fully passed
            state.status = "passed"
        elif minutes_to_start <= 0:
            # During event or within flat_after window
            state.status = "active"
            state.in_flat_after = True
            state.trading_halted = event.flat_after_min > 0 and abs(minutes_to_start) <= event.flat_after_min
        elif minutes_to_start <= event.flat_before_min:
            # Within flat_before window
            state.status = "active"
            state.in_flat_before = True
            state.trading_halted = event.flat_before_min > 0
        else:
            # Upcoming
            state.status = "upcoming"

        return state

    # ── Warning Publishing ──────────────────────

    async def _publish_warning(self, state: EventState) -> None:
        """Publish event warning via Redis PUB/SUB.

        Args:
            state: EventState for the upcoming event.
        """
        if self._redis is None or not self._redis.is_initialized:
            return

        message = {
            "event": "event_warning",
            "event_id": state.event.event_id,
            "event_name": state.event.event_name,
            "category": state.event.category,
            "importance": state.event.importance,
            "minutes_to_start": state.minutes_to_start,
            "flat_before_min": state.event.flat_before_min,
            "flat_after_min": state.event.flat_after_min,
            "lot_scale": state.event.lot_scale,
            "confidence_discount": state.event.confidence_discount,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        try:
            await self._redis.publish(REDIS_EVENT_CHANNEL, json.dumps(message))
            self._stats["warnings_published"] += 1
            logger.info(
                "Event warning published: %s (importance=%d, T-%.0fmin)",
                state.event.event_name, state.event.importance, state.minutes_to_start,
            )
        except Exception as exc:
            logger.error("Failed to publish event warning: %s", exc)

    async def _trigger_halt(self, state: EventState) -> None:
        """Trigger trading halt for an event.

        Args:
            state: EventState with trading_halted=True.
        """
        self._stats["halts_triggered"] += 1

        logger.warning(
            "Trading halt triggered: %s (category=%s, importance=%d, lot_scale=%.1f)",
            state.event.event_name, state.event.category,
            state.event.importance, state.event.lot_scale,
        )

        # Publish circuit breaker event
        if self._redis is not None and self._redis.is_initialized:
            try:
                await self._redis.publish(
                    REDIS_CIRCUIT_BREAKER_CHANNEL,
                    json.dumps({
                        "event": "trading_halt",
                        "event_name": state.event.event_name,
                        "category": state.event.category,
                        "importance": state.event.importance,
                        "lot_scale": state.event.lot_scale,
                        "confidence_discount": state.event.confidence_discount,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }),
                )
            except Exception as exc:
                logger.error("Failed to publish trading halt: %s", exc)

    # ── Redis Cache ─────────────────────────────

    async def _update_redis_active(self) -> None:
        """Update Redis event:active cache with current active events."""
        if self._redis is None or not self._redis.is_initialized:
            return

        active_list: list[dict] = []
        for category, states in self._category_states.items():
            for state in states:
                if state.status == "active":
                    active_list.append({
                        "event_id": state.event.event_id,
                        "event_name": state.event.event_name,
                        "category": state.event.category,
                        "importance": state.event.importance,
                        "minutes_to_start": state.minutes_to_start,
                        "in_flat_before": state.in_flat_before,
                        "in_flat_after": state.in_flat_after,
                        "trading_halted": state.trading_halted,
                        "lot_scale": state.event.lot_scale,
                        "confidence_discount": state.event.confidence_discount,
                    })

        try:
            await self._redis.set(REDIS_EVENT_ACTIVE, json.dumps(active_list))
            logger.debug("Redis event:active updated: %d active events", len(active_list))
        except Exception as exc:
            logger.error("Redis event:active update failed: %s", exc)

    # ── Risk Assessment ─────────────────────────

    async def get_event_restrictions(
        self, category: str = "all", symbol: str = ""
    ) -> dict[str, Any]:
        """Get current event trading restrictions for a category/symbol.

        Args:
            category: Asset category.
            symbol: Trading symbol (for category resolution).

        Returns:
            Dict with lot_scale, confidence_discount, halt, and active events.
        """
        now = datetime.now(timezone.utc)
        states = self._check_category(category, now)

        active_states = [s for s in states if s.status == "active"]
        worst_importance = max((s.event.importance for s in active_states), default=0)
        min_lot_scale = min((s.event.lot_scale for s in active_states), default=1.0)
        max_conf_discount = max((s.event.confidence_discount for s in active_states), default=0.0)
        trading_halted = any(s.trading_halted for s in active_states)

        return {
            "event_risk_level": self._importance_to_risk(worst_importance),
            "event_restriction": "halt" if trading_halted else ("scaled" if min_lot_scale < 1.0 else "none"),
            "event_lot_scale": 0.0 if trading_halted else min_lot_scale,
            "confidence_discount": max_conf_discount,
            "active_events": [
                {
                    "event_name": s.event.event_name,
                    "importance": s.event.importance,
                    "minutes_to_start": s.minutes_to_start,
                }
                for s in active_states[:5]  # Top 5
            ],
        }

    @staticmethod
    def _importance_to_risk(importance: int) -> int:
        """Map event importance to risk level 0-10.

        Args:
            importance: Event importance (1=MEDIUM, 2=HIGH, 3=CRITICAL).

        Returns:
            Risk score 0-10.
        """
        mapping = {0: 0, 1: 3, 2: 6, 3: 9}
        return mapping.get(importance, 0)

    # ── Query ───────────────────────────────────

    async def get_upcoming_events(
        self, category: str = "all", hours_ahead: int = 24
    ) -> list[dict]:
        """Get upcoming events within a time window.

        Args:
            category: Asset category filter.
            hours_ahead: Look-ahead window in hours.

        Returns:
            List of upcoming event dicts.
        """
        if not self._events:
            await self.load_events()

        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours_ahead)
        upcoming: list[dict] = []

        for event in self._events.values():
            if event.category not in (category, "all"):
                continue
            if event.event_date < now or event.event_date > cutoff:
                continue

            upcoming.append({
                "event_id": event.event_id,
                "event_name": event.event_name,
                "category": event.category,
                "event_date": event.event_date.isoformat(),
                "importance": event.importance,
                "importance_label": {1: "MEDIUM", 2: "HIGH", 3: "CRITICAL"}.get(event.importance, "UNKNOWN"),
                "flat_before_min": event.flat_before_min,
                "flat_after_min": event.flat_after_min,
                "lot_scale": event.lot_scale,
                "confidence_discount": event.confidence_discount,
                "minutes_to_start": round((event.event_date - now).total_seconds() / 60, 1),
            })

        upcoming.sort(key=lambda e: e["minutes_to_start"])
        return upcoming

    # ── Config ──────────────────────────────────

    async def load_config(self) -> None:
        """Load calendar parameters from config_provider."""
        if self._config is None:
            return

        try:
            self._warning_advance_min = await self._config.get_int(
                "event_warning_advance_min", DEFAULT_WARNING_ADVANCE_MIN
            )
            self._check_interval = await self._config.get_int(
                "event_check_interval_sec", DEFAULT_CHECK_INTERVAL
            )
            logger.info("EventCalendar config loaded: warning_advance=%dmin, interval=%ds",
                       self._warning_advance_min, self._check_interval)
        except Exception as exc:
            logger.warning("EventCalendar config load failed: %s", exc)

    # ── Stats ───────────────────────────────────

    def get_stats(self) -> dict:
        """Get calendar statistics."""
        return {
            **self._stats,
            "events_loaded": len(self._events),
        }

    @property
    def event_count(self) -> int:
        """Number of loaded events."""
        return len(self._events)
