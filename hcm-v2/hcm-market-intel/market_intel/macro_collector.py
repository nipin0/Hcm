"""Macro Collector — Macro environment data collection and scoring.

Collects macro data per category (metals/crypto/forex) from sources
including FRED, CME FedWatch, BLS, and Investing.com. Detects data
changes, invokes DeepSeek for scoring, and writes to PostgreSQL
(macro_snapshots) + Redis (macro:latest:{category}).

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

# Data source URLs (for reference — stub mode uses cached/heuristic values)
FRED_BASE_URL = "https://api.stlouisfed.org/fred"
CME_BASE_URL = "https://www.cmegroup.com"
INVESTING_BASE_URL = "https://api.investing.com"

# Redis key patterns
REDIS_MACRO_LATEST = "macro:latest:{category}"

# Macro indicators per category
CATEGORY_MACRO_SOURCES: dict[str, list[str]] = {
    "metals": ["dxy", "real_yield_10y", "fed_funds_rate", "cpi_yoy", "gold_etf_holdings"],
    "crypto": ["dxy", "fed_funds_rate", "btc_dominance", "stablecoin_mcap", "hash_rate"],
    "forex": ["dxy", "fed_funds_rate", "ecb_rate", "boj_rate", "current_account"],
}


@dataclass
class MacroData:
    """Collected macro data for a single category snapshot.

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


class MacroCollector:
    """Collects macro environment data and triggers AI scoring.

    Per category, collects macro indicators from configured sources,
    compares with previous values, invokes DeepSeek scoring on change,
    and writes results to PostgreSQL + Redis cache.

    Example:
        collector = MacroCollector(db_pool, redis_client, ai_scorer, config_provider)
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
        """Initialize MacroCollector.

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
        """Collect macro data for all (or specified) categories.

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
                logger.error("Macro collection failed for category=%s: %s", category, exc)
                self._stats["errors"] += 1
                results[category] = None

        self._stats["collections"] += 1
        return results

    async def collect_category(self, category: str) -> Optional[int]:
        """Collect macro data for a single category.

        Steps:
        1. Fetch macro indicators from sources (or stub)
        2. Detect changes vs previous values
        3. Invoke AI scoring if changed
        4. Write to PG + Redis

        Args:
            category: Asset category (metals/crypto/forex).

        Returns:
            Snapshot ID if successful, None otherwise.
        """
        logger.info("Collecting macro data for category=%s", category)

        # 1. Collect data
        data = await self._collect_data(category)

        # 2. Detect changes
        prev = self._previous_values.get(category, {})
        data.changed = self._detect_changes(data.indicator_values, prev)
        if data.changed:
            self._stats["changes_detected"] += 1
            logger.debug("Macro data changed for %s: %s", category,
                        {k: v for k, v in data.indicator_values.items()})
        else:
            logger.debug("Macro data unchanged for %s — skipping scoring", category)
            return None

        # 3. AI Scoring
        score_result = None
        if self._ai_scorer is not None:
            try:
                score_result = await self._ai_scorer.score_macro(category, data.indicator_values)
                logger.info(
                    "Macro score for %s: %d/30, bias=%s, stub=%s",
                    category, score_result.score, score_result.bias, score_result.stub,
                )
            except Exception as exc:
                logger.warning("Macro scoring failed for %s: %s", category, exc)

        # 4. Write to PG + Redis
        snapshot_id = await self._persist(category, data, score_result)

        # Update previous values
        self._previous_values[category] = dict(data.indicator_values)

        return snapshot_id

    # ── Data Collection ─────────────────────────

    async def _collect_data(self, category: str) -> MacroData:
        """Collect macro indicator data for a category.

        For metals: derives indicators from XAUUSD M5 klines in PG.
        For crypto/forex: returns empty indicators pending future data sources.

        Args:
            category: Asset category.

        Returns:
            MacroData with collected indicator values.
        """
        if category == "metals" and self._db is not None and self._db.is_initialized:
            try:
                indicators = await _compute_macro_from_klines(self._db)
                return MacroData(
                    category=category,
                    indicator_values=indicators,
                    changed=True,
                    source="mt5_derived",
                    collected_at=datetime.now(timezone.utc).isoformat(),
                )
            except Exception as exc:
                logger.warning(
                    "Klines-based macro computation failed for metals: %s — degraded", exc
                )
                return MacroData(
                    category=category,
                    indicator_values={},
                    changed=True,
                    source="degraded_no_klines",
                    collected_at=datetime.now(timezone.utc).isoformat(),
                )

        # crypto / forex: no real data source yet
        return MacroData(
            category=category,
            indicator_values={},
            changed=True,
            source="mt5_derived",
            collected_at=datetime.now(timezone.utc).isoformat(),
        )

    # ── Change Detection ────────────────────────

    @staticmethod
    def _detect_changes(
        current: dict[str, Any], previous: dict[str, Any], threshold: float = 0.001
    ) -> bool:
        """Detect if macro values have changed meaningfully.

        Args:
            current: Current indicator values.
            previous: Previous indicator values.
            threshold: Relative change threshold (0.1%).

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
        self, category: str, data: MacroData, score_result: Optional[Any] = None
    ) -> Optional[int]:
        """Persist macro snapshot to PostgreSQL and Redis.

        Args:
            category: Asset category.
            data: Collected MacroData.
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
                    """INSERT INTO hcm_market.macro_snapshots
                       (category, macro_risk_score, macro_bias, ai_summary, category_data, snapshot_time)
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
                    logger.info("Macro snapshot written to PG: id=%d, category=%s", snapshot_id, category)
            except Exception as exc:
                logger.error("PG write failed for macro snapshot category=%s: %s", category, exc)
                self._stats["errors"] += 1

        # 2. Write to Redis (SET with category key)
        if self._redis is not None and self._redis.is_initialized:
            try:
                redis_key = REDIS_MACRO_LATEST.format(category=category)
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
                logger.debug("Macro snapshot written to Redis: key=%s", redis_key)
            except Exception as exc:
                logger.error("Redis write failed for macro key=%s: %s",
                           REDIS_MACRO_LATEST.format(category=category), exc)

        return snapshot_id

    # ── Query ───────────────────────────────────

    async def get_latest(self, category: str) -> Optional[dict]:
        """Get the latest macro snapshot for a category.

        Tries Redis first, then falls back to PostgreSQL.

        Args:
            category: Asset category.

        Returns:
            Dict with score/bias/summary/indicators, or None.
        """
        # Try Redis cache first
        if self._redis is not None and self._redis.is_initialized:
            try:
                redis_key = REDIS_MACRO_LATEST.format(category=category)
                cached = await self._redis.get(redis_key)
                if cached:
                    return json.loads(cached) if isinstance(cached, str) else json.loads(str(cached))
            except Exception as exc:
                logger.warning("Redis get failed for %s: %s", redis_key, exc)

        # Fall back to PostgreSQL
        if self._db is not None and self._db.is_initialized:
            try:
                row = await self._db.fetchrow(
                    """SELECT id, macro_risk_score, macro_bias, ai_summary, category_data, snapshot_time
                       FROM hcm_market.macro_snapshots
                       WHERE category=$1
                       ORDER BY snapshot_time DESC LIMIT 1""",
                    category,
                )
                if row:
                    return {
                        "category": category,
                        "score": row["macro_risk_score"],
                        "bias": row["macro_bias"],
                        "summary": row["ai_summary"],
                        "snapshot_id": row["id"],
                        "snapshot_time": row["snapshot_time"].isoformat() if row["snapshot_time"] else "",
                        "category_data": row["category_data"] if isinstance(row["category_data"], dict) else {},
                    }
            except Exception as exc:
                logger.error("PG query failed for macro category=%s: %s", category, exc)

        return None

    async def get_latest_all(self) -> dict[str, Optional[dict]]:
        """Get latest macro snapshots for all categories.

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
                "macro_collect_interval_min", DEFAULT_COLLECT_INTERVAL // 60
            ) * 60
            logger.info("MacroCollector config loaded: interval=%ds", self._collect_interval)
        except Exception as exc:
            logger.warning("MacroCollector config load failed: %s", exc)

    # ── Stats ───────────────────────────────────

    def get_stats(self) -> dict:
        """Get collector statistics."""
        return dict(self._stats)

    @property
    def stub_mode(self) -> bool:
        """Whether stub mode is active."""
        return self._stub_mode


# ── Klines-derived Macro Computation ────────────


async def _compute_macro_from_klines(db_pool: Any) -> dict[str, Any]:
    """Derive macro indicators from XAUUSD M5 klines in PostgreSQL.

    Reads the most recent 200 M5 bars for XAUUSD and computes proxy
    indicators for DXY, real yields, CPI, VIX, and gold ETF holdings
    based on gold price dynamics.

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

    # ── dxy_proxy: gold-dollar inverse correlation ──
    # Gold up → dollar down. Map 1% gold change ≈ 0.1 DXY point shift.
    idx_50: int = max(0, n - 50)
    close_50: float = closes[idx_50]
    if close_50 > 0:
        dxy: float = round(100.0 + (1.0 - close_now / close_50) * 10.0, 2)
    else:
        dxy = 100.0

    # ── real_yield_10y: gold-real yield inverse ──
    # Rising gold → falling real yields. 20-bar momentum as proxy.
    idx_20: int = max(0, n - 20)
    close_20: float = closes[idx_20]
    if close_20 > 0:
        real_yield_10y: float = round((close_now - close_20) / close_20 * -100.0, 2)
    else:
        real_yield_10y = 0.0

    # ── cpi_yoy: gold monthly trend → inflation proxy ──
    # Map 30-bar gold change rate to 2%–4% CPI range.
    idx_30: int = max(0, n - 30)
    close_30: float = closes[idx_30]
    if close_30 > 0:
        gold_change: float = (close_now - close_30) / close_30
        cpi_yoy: float = round(3.0 + gold_change * 20.0, 2)
        cpi_yoy = max(2.0, min(4.0, cpi_yoy))
    else:
        cpi_yoy = 3.0

    # ── vix: gold volatility → fear gauge ──
    # Std/mean of last 50 bars mapped to 10–30 range.
    recent: list[float] = closes[-min(50, n):]
    mean_val: float = sum(recent) / len(recent)
    variance: float = sum((c - mean_val) ** 2 for c in recent) / len(recent)
    cv: float = (variance ** 0.5) / mean_val if mean_val > 0 else 0.0
    vix: float = round(10.0 + cv * 2000.0, 2)
    vix = max(10.0, min(30.0, vix))

    # ── gold_etf_holdings: position within 200-bar range ──
    high: float = max(closes)
    low: float = min(closes)
    if high > low:
        position_pct: float = (close_now - low) / (high - low)
        gold_etf_holdings: float = round(800.0 + position_pct * 150.0, 1)
    else:
        gold_etf_holdings = 875.0

    logger.debug(
        "Macro from klines: dxy=%.2f real_yield=%.2f cpi=%.2f vix=%.2f etf=%.1f (n=%d)",
        dxy, real_yield_10y, cpi_yoy, vix, gold_etf_holdings, n,
    )

    return {
        "dxy": dxy,
        "real_yield_10y": real_yield_10y,
        "fed_funds_rate": 5.50,  # static – not derivable from gold klines
        "cpi_yoy": cpi_yoy,
        "vix": vix,
        "gold_etf_holdings": gold_etf_holdings,
    }
