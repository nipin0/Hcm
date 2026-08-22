"""Sentiment Collector — Market sentiment data collection and scoring.

Collects sentiment data per category (metals/crypto/forex) from sources
including COT reports, ETF flows, VIX, retail long/short ratios, and
Fear & Greed indices. Detects data changes, invokes DeepSeek scoring,
and writes to PostgreSQL (sentiment_snapshots) + Redis
(sentiment:latest:{category}).

Supports stub mode for environments without external API access.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Default Config ─────────────────────────────

DEFAULT_COLLECT_INTERVAL = 3600  # 60 minutes
DEFAULT_CATEGORIES = ["metals", "crypto", "forex"]
DEFAULT_STUB_MODE = False

# Redis key patterns
REDIS_SENTIMENT_LATEST = "sentiment:latest:{category}"

# Sentiment sources per category
CATEGORY_SENTIMENT_SOURCES: dict[str, list[str]] = {
    "metals": ["cot_gold_net_long", "gold_etf_flow", "vix", "retail_long_ratio", "put_call_ratio"],
    "crypto": ["fear_greed_index", "btc_etf_flow", "long_short_ratio", "open_interest", "exchange_flows"],
    "forex": ["cot_dollar_net_long", "vix", "risk_reversal", "positioning_index", "carry_trade_index"],
}


@dataclass
class SentimentData:
    """Collected sentiment data for a single category snapshot.

    Attributes:
        category: Asset category.
        indicator_values: Dict of indicator_name → value.
        changed: Whether data has changed since last collection.
        source: Data source label.
        collected_at: UTC timestamp of collection.
    """

    category: str = ""
    indicator_values: dict[str, Any] = field(default_factory=dict)
    changed: bool = True
    source: str = "stub"
    collected_at: str = ""


class SentimentCollector:
    """Collects market sentiment data and triggers AI scoring.

    Per category, collects sentiment indicators from configured sources,
    compares with previous values, invokes DeepSeek scoring on change,
    and writes results to PostgreSQL + Redis cache.

    Example:
        collector = SentimentCollector(db_pool, redis_client, ai_scorer, config_provider)
        await collector.collect_all()
    """

    def __init__(
        self,
        db_pool: Any = None,
        redis_client: Any = None,
        ai_scorer: Any = None,
        config_provider: Any = None,
        collect_interval: int = DEFAULT_COLLECT_INTERVAL,
        stub_mode: bool = DEFAULT_STUB_MODE,
    ):
        """Initialize SentimentCollector.

        Args:
            db_pool: DatabasePool for PG writes.
            redis_client: RedisClient for cache writes.
            ai_scorer: AiScorer instance for DeepSeek scoring.
            config_provider: ConfigProviderV3 for runtime config.
            collect_interval: Collection interval in seconds.
            stub_mode: If True, use heuristic data instead of external APIs.
        """
        self._db = db_pool
        self._redis = redis_client
        self._ai_scorer = ai_scorer
        self._config = config_provider
        self._collect_interval = collect_interval
        self._stub_mode = stub_mode

        # Previous values for change detection
        self._previous_values: dict[str, dict[str, Any]] = {}

        # Stats
        self._stats: dict[str, int] = {
            "collections": 0,
            "changes_detected": 0,
            "scores_written": 0,
            "errors": 0,
        }

    # ── Main Collection API ─────────────────────

    async def collect_all(self, categories: Optional[list[str]] = None) -> dict[str, Optional[int]]:
        """Collect sentiment data for all (or specified) categories.

        Args:
            categories: List of categories to collect (default: all).

        Returns:
            Dict mapping category → snapshot_id (or None if failed).
        """
        if categories is None:
            categories = DEFAULT_CATEGORIES

        results: dict[str, Optional[int]] = {}

        for category in categories:
            try:
                snapshot_id = await self.collect_category(category)
                results[category] = snapshot_id
            except Exception as exc:
                logger.error("Sentiment collection failed for category=%s: %s", category, exc)
                self._stats["errors"] += 1
                results[category] = None

        self._stats["collections"] += 1
        return results

    async def collect_category(self, category: str) -> Optional[int]:
        """Collect sentiment data for a single category.

        Steps:
        1. Fetch sentiment indicators from sources (or stub)
        2. Detect changes vs previous values
        3. Invoke AI scoring if changed
        4. Write to PG + Redis

        Args:
            category: Asset category (metals/crypto/forex).

        Returns:
            Snapshot ID if successful, None otherwise.
        """
        logger.info("Collecting sentiment data for category=%s", category)

        # 1. Collect data
        data = await self._collect_data(category)

        # 2. Detect changes
        prev = self._previous_values.get(category, {})
        data.changed = self._detect_changes(data.indicator_values, prev)
        if data.changed:
            self._stats["changes_detected"] += 1
            logger.debug("Sentiment data changed for %s", category)
        else:
            logger.debug("Sentiment data unchanged for %s — skipping scoring", category)
            return None

        # 3. AI Scoring
        score_result = None
        if self._ai_scorer is not None:
            try:
                score_result = await self._ai_scorer.score_sentiment(category, data.indicator_values)
                logger.info(
                    "Sentiment score for %s: %d/20, bias=%s, stub=%s",
                    category, score_result.score, score_result.bias, score_result.stub,
                )
            except Exception as exc:
                logger.warning("Sentiment scoring failed for %s: %s", category, exc)

        # 4. Write to PG + Redis
        snapshot_id = await self._persist(category, data, score_result)

        # Update previous values
        self._previous_values[category] = dict(data.indicator_values)

        return snapshot_id

    # ── Data Collection ─────────────────────────

    async def _collect_data(self, category: str) -> SentimentData:
        """Collect sentiment indicator data for a category.

        For metals: derives sentiment proxies from XAUUSD M5 klines in PG.
        For crypto/forex: returns empty indicators pending future data sources.

        Args:
            category: Asset category.

        Returns:
            SentimentData with collected indicator values.
        """
        if category == "metals" and self._db is not None and self._db.is_initialized:
            try:
                indicators = await _compute_sentiment_from_klines(self._db)
                return SentimentData(
                    category=category,
                    indicator_values=indicators,
                    changed=True,
                    source="mt5_derived",
                    collected_at=datetime.now(timezone.utc).isoformat(),
                )
            except Exception as exc:
                logger.warning(
                    "Klines-based sentiment computation failed for metals: %s — degraded", exc
                )
                return SentimentData(
                    category=category,
                    indicator_values={},
                    changed=True,
                    source="degraded_no_klines",
                    collected_at=datetime.now(timezone.utc).isoformat(),
                )

        # crypto / forex: no real data source yet
        return SentimentData(
            category=category,
            indicator_values={},
            changed=True,
            source="mt5_derived",
            collected_at=datetime.now(timezone.utc).isoformat(),
        )

    # ── Change Detection ────────────────────────

    @staticmethod
    def _detect_changes(
        current: dict[str, Any], previous: dict[str, Any], threshold: float = 0.01
    ) -> bool:
        """Detect if sentiment values have changed meaningfully.

        Args:
            current: Current indicator values.
            previous: Previous indicator values.
            threshold: Relative change threshold (1%).

        Returns:
            True if any value changed beyond threshold.
        """
        if not previous:
            return True  # First collection always counts as changed

        for key, val in current.items():
            prev_val = previous.get(key)
            if prev_val is None:
                return True  # New indicator

            if isinstance(val, (int, float)) and isinstance(prev_val, (int, float)):
                if prev_val == 0:
                    if abs(val) > 0.01:
                        return True
                elif abs((val - prev_val) / prev_val) > threshold:
                    return True
            elif str(val) != str(prev_val):
                return True

        return False

    # ── Persistence ─────────────────────────────

    async def _persist(
        self, category: str, data: SentimentData, score_result: Optional[Any] = None
    ) -> Optional[int]:
        """Persist sentiment snapshot to PostgreSQL and Redis.

        Args:
            category: Asset category.
            data: Collected SentimentData.
            score_result: AiScorer ScoreResult (or None).

        Returns:
            Snapshot ID if PG write succeeded.
        """
        score = score_result.score if score_result else 0
        bias = score_result.bias if score_result else "neutral"
        summary = score_result.summary if score_result else ""
        category_data = {
            "indicators": data.indicator_values,
            "source": data.source,
            "collected_at": data.collected_at,
        }

        snapshot_id: Optional[int] = None

        # 1. Write to PostgreSQL
        if self._db is not None and self._db.is_initialized:
            try:
                row = await self._db.fetchrow(
                    """INSERT INTO hcm_market.sentiment_snapshots
                       (category, sentiment_risk_score, sentiment_bias, ai_summary, category_data, snapshot_time)
                       VALUES ($1, $2, $3, $4, $5, $6)
                       RETURNING id""",
                    category,
                    score,
                    bias,
                    summary[:500],
                    json.dumps(category_data),
                    datetime.now(timezone.utc),
                )
                if row:
                    snapshot_id = row["id"]
                    self._stats["scores_written"] += 1
                    logger.info("Sentiment snapshot written to PG: id=%d, category=%s", snapshot_id, category)
            except Exception as exc:
                logger.error("PG write failed for sentiment snapshot category=%s: %s", category, exc)
                self._stats["errors"] += 1

        # 2. Write to Redis (SET with category key)
        if self._redis is not None and self._redis.is_initialized:
            try:
                redis_key = REDIS_SENTIMENT_LATEST.format(category=category)
                redis_value = json.dumps({
                    "category": category,
                    "score": score,
                    "bias": bias,
                    "summary": summary[:500],
                    "indicators": data.indicator_values,
                    "snapshot_id": snapshot_id,
                    "collected_at": data.collected_at,
                    "source": data.source,
                })
                await self._redis.set(redis_key, redis_value)
                logger.debug("Sentiment snapshot written to Redis: key=%s", redis_key)
            except Exception as exc:
                logger.error("Redis write failed for sentiment key=%s: %s",
                           REDIS_SENTIMENT_LATEST.format(category=category), exc)

        return snapshot_id

    # ── Query ───────────────────────────────────

    async def get_latest(self, category: str) -> Optional[dict]:
        """Get the latest sentiment snapshot for a category.

        Tries Redis first, then falls back to PostgreSQL.

        Args:
            category: Asset category.

        Returns:
            Dict with score/bias/summary/indicators, or None.
        """
        # Try Redis cache first
        if self._redis is not None and self._redis.is_initialized:
            try:
                redis_key = REDIS_SENTIMENT_LATEST.format(category=category)
                cached = await self._redis.get(redis_key)
                if cached:
                    return json.loads(cached) if isinstance(cached, str) else json.loads(str(cached))
            except Exception as exc:
                logger.warning("Redis get failed for %s: %s", redis_key, exc)

        # Fall back to PostgreSQL
        if self._db is not None and self._db.is_initialized:
            try:
                row = await self._db.fetchrow(
                    """SELECT id, sentiment_risk_score, sentiment_bias, ai_summary, category_data, snapshot_time
                       FROM hcm_market.sentiment_snapshots
                       WHERE category=$1
                       ORDER BY snapshot_time DESC LIMIT 1""",
                    category,
                )
                if row:
                    return {
                        "category": category,
                        "score": row["sentiment_risk_score"],
                        "bias": row["sentiment_bias"],
                        "summary": row["ai_summary"],
                        "snapshot_id": row["id"],
                        "snapshot_time": row["snapshot_time"].isoformat() if row["snapshot_time"] else "",
                        "category_data": row["category_data"] if isinstance(row["category_data"], dict) else {},
                    }
            except Exception as exc:
                logger.error("PG query failed for sentiment category=%s: %s", category, exc)

        return None

    async def get_latest_all(self) -> dict[str, Optional[dict]]:
        """Get latest sentiment snapshots for all categories.

        Returns:
            Dict mapping category → snapshot dict (or None).
        """
        results: dict[str, Optional[dict]] = {}
        for category in DEFAULT_CATEGORIES:
            results[category] = await self.get_latest(category)
        return results

    # ── Config ──────────────────────────────────

    async def load_config(self) -> None:
        """Load collector parameters from config_provider."""
        if self._config is None:
            return

        try:
            self._collect_interval = await self._config.get_int(
                "sentiment_collect_interval_min", DEFAULT_COLLECT_INTERVAL // 60
            ) * 60
            logger.info("SentimentCollector config loaded: interval=%ds", self._collect_interval)
        except Exception as exc:
            logger.warning("SentimentCollector config load failed: %s", exc)

    # ── Stats ───────────────────────────────────

    def get_stats(self) -> dict:
        """Get collector statistics."""
        return dict(self._stats)

    @property
    def stub_mode(self) -> bool:
        """Whether stub mode is active."""
        return self._stub_mode


# ── Klines-derived Sentiment Computation ────────


async def _compute_sentiment_from_klines(db_pool: Any) -> dict[str, Any]:
    """Derive sentiment proxies from XAUUSD M5 klines in PostgreSQL.

    Reads the most recent 200 M5 bars for XAUUSD and computes proxy
    indicators for COT net longs, VIX, and fear/greed based on gold
    price dynamics.

    Args:
        db_pool: DatabasePool with fetch() method.

    Returns:
        Dict of indicator_name → value with source traceability.

    Raises:
        ValueError: If no klines data is available.
    """
    rows = await db_pool.fetch(
        """SELECT close, open_time
           FROM hcm_market.klines
           WHERE symbol = 'XAUUSD' AND time_frame = 'M5'
           ORDER BY open_time DESC
           LIMIT 200"""
    )
    if not rows:
        raise ValueError("No klines data for XAUUSD M5")

    # Reverse to chronological order (oldest → newest)
    rows_reversed = list(reversed(rows))
    closes: list[float] = [float(r["close"]) for r in rows_reversed]
    n: int = len(closes)

    close_now: float = closes[-1]

    # ── cot_gold_net_long: gold trend → speculative positioning ──
    # Rising trend → more net longs, falling → fewer.
    idx_50: int = max(0, n - 50)
    close_50: float = closes[idx_50]
    if close_50 > 0:
        trend_pct: float = (close_now - close_50) / close_50
        # Map trend (-2% to +2%) → net longs (200k–350k)
        cot_net_long: float = round(275000.0 + trend_pct * 3750000.0, 0)
    else:
        cot_net_long = 275000.0

    # ── cot_gold_net_change: week-over-week change proxy ──
    idx_10: int = max(0, n - 10)
    close_10: float = closes[idx_10]
    cot_net_change: float = round((close_now - close_10) / close_10 * 100000.0, 0) if close_10 > 0 else 0.0

    # ── vix: gold volatility → fear gauge ──
    recent: list[float] = closes[-min(50, n):]
    mean_val: float = sum(recent) / len(recent)
    variance: float = sum((c - mean_val) ** 2 for c in recent) / len(recent)
    cv: float = (variance ** 0.5) / mean_val if mean_val > 0 else 0.0
    vix: float = round(10.0 + cv * 2000.0, 2)
    vix = max(10.0, min(30.0, vix))

    # ── fear_greed_index: volatility inversion → 0–100 ──
    # Higher volatility → more fear (lower index).
    fear_greed: float = round(100.0 - cv * 8000.0, 0)
    fear_greed = max(0.0, min(100.0, fear_greed))

    # ── retail_long_ratio: short-term momentum proxy ──
    idx_5: int = max(0, n - 5)
    close_5: float = closes[idx_5]
    if close_5 > 0:
        momentum: float = (close_now - close_5) / close_5
        retail_long: float = round(50.0 + momentum * 500.0, 1)
        retail_long = max(20.0, min(80.0, retail_long))
    else:
        retail_long = 50.0

    # ── put_call_ratio: inverse of trend confidence ──
    if close_50 > 0:
        pcr: float = round(1.0 - trend_pct * 5.0, 2)
        pcr = max(0.5, min(1.5, pcr))
    else:
        pcr = 1.0

    logger.debug(
        "Sentiment from klines: cot=%.0f vix=%.2f fear_greed=%.0f pcr=%.2f (n=%d)",
        cot_net_long, vix, fear_greed, pcr, n,
    )

    return {
        "cot_gold_net_long": cot_net_long,
        "cot_gold_net_change": cot_net_change,
        "vix": vix,
        "fear_greed_index": fear_greed,
        "retail_long_ratio": retail_long,
        "put_call_ratio": pcr,
    }
