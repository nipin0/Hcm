"""Liquidity Analyzer — Real-time spread/depth monitoring.

Monitors bid-ask spread and market depth for active symbols,
detects anomalous liquidity conditions, and writes results to
Redis (liquidity:current with 5s TTL).

Per PRD §6.3.4, liquidity analysis covers:
- Bid-Ask spread monitoring (absolute pips + percentage)
- Market depth (bid/ask volumes at multiple levels)
- Spread anomaly detection (vs historical average)
- Liquidity risk scoring for signal confidence adjustment
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

DEFAULT_CACHE_TTL_SEC = 5
DEFAULT_CHECK_INTERVAL = 5  # seconds
DEFAULT_SPREAD_MAX_PIPS = 5.0
DEFAULT_DEPTH_LEVELS = 5
DEFAULT_ANOMALY_MULTIPLIER = 2.0

# Redis keys
REDIS_LIQUIDITY_CURRENT = "liquidity:current"
REDIS_LIQUIDITY_HISTORY = "liquidity:history:{symbol}"

# Liquidity levels for risk scoring
LIQUIDITY_LEVELS = {
    "abundant": {"max_spread_mult": 1.0, "confidence_mult": 1.0},
    "normal": {"max_spread_mult": 1.5, "confidence_mult": 0.95},
    "thin": {"max_spread_mult": 2.5, "confidence_mult": 0.85},
    "danger": {"max_spread_mult": 5.0, "confidence_mult": 0.60},
}


@dataclass
class DepthLevel:
    """Market depth at a single price level.

    Attributes:
        level: Depth level index (0 = best bid/ask).
        bid_price: Bid price at this level.
        bid_volume: Bid volume in lots.
        ask_price: Ask price at this level.
        ask_volume: Ask volume in lots.
    """

    level: int = 0
    bid_price: float = 0.0
    bid_volume: float = 0.0
    ask_price: float = 0.0
    ask_volume: float = 0.0


@dataclass
class LiquiditySnapshot:
    """Real-time liquidity snapshot for a symbol.

    Attributes:
        symbol: Trading symbol.
        bid: Current best bid price.
        ask: Current best ask price.
        spread: Bid-ask spread in raw price units.
        spread_pips: Spread in pips.
        spread_pct: Spread as percentage of price.
        depth_bid_total: Total bid volume across N levels.
        depth_ask_total: Total ask volume across N levels.
        depth_imbalance: Bid/Ask volume imbalance (-1.0 to 1.0).
        depth_levels: List of depth levels.
        liquidity_level: "abundant", "normal", "thin", or "danger".
        is_anomalous: Whether spread exceeds historical threshold.
        spread_multiplier: Current spread / average spread ratio.
        timestamp: UTC snapshot time.
    """

    symbol: str = ""
    bid: float = 0.0
    ask: float = 0.0
    spread: float = 0.0
    spread_pips: float = 0.0
    spread_pct: float = 0.0
    depth_bid_total: float = 0.0
    depth_ask_total: float = 0.0
    depth_imbalance: float = 0.0
    depth_levels: list[DepthLevel] = field(default_factory=list)
    liquidity_level: str = "normal"
    is_anomalous: bool = False
    spread_multiplier: float = 1.0
    timestamp: str = ""


class LiquidityAnalyzer:
    """Real-time liquidity and spread monitor.

    Monitors bid-ask spread and market depth for active symbols,
    detects anomalous liquidity conditions, computes liquidity risk
    scores, and caches results in Redis with short TTL.

    Example:
        analyzer = LiquidityAnalyzer(redis_client, config_provider)
        snapshot = await analyzer.analyze("XAUUSD", bids, asks)
        await analyzer.cache_snapshot(snapshot)
    """

    def __init__(
        self,
        redis_client: Any = None,
        config_provider: Any = None,
        cache_ttl: int = DEFAULT_CACHE_TTL_SEC,
        check_interval: int = DEFAULT_CHECK_INTERVAL,
        spread_max_pips: float = DEFAULT_SPREAD_MAX_PIPS,
        depth_levels: int = DEFAULT_DEPTH_LEVELS,
        anomaly_multiplier: float = DEFAULT_ANOMALY_MULTIPLIER,
    ):
        """Initialize LiquidityAnalyzer.

        Args:
            redis_client: RedisClient for cache writes.
            config_provider: ConfigProviderV3 for runtime config.
            cache_ttl: Redis cache TTL in seconds.
            check_interval: Liquidity check interval in seconds.
            spread_max_pips: Max acceptable spread in pips.
            depth_levels: Number of depth levels to track.
            anomaly_multiplier: Spread multiplier to flag as anomalous.
        """
        self._redis = redis_client
        self._config = config_provider
        self._cache_ttl = cache_ttl
        self._check_interval = check_interval
        self._spread_max_pips = spread_max_pips
        self._depth_levels = depth_levels
        self._anomaly_multiplier = anomaly_multiplier

        # Rolling average spread per symbol (for anomaly detection)
        self._avg_spreads: dict[str, float] = {}

        # Latest snapshots per symbol
        self._snapshots: dict[str, LiquiditySnapshot] = {}

        # Stats
        self._stats: dict[str, int] = {
            "snapshots": 0,
            "anomalies": 0,
            "cache_writes": 0,
            "errors": 0,
        }

    # ── Main API ────────────────────────────────

    async def analyze(
        self,
        symbol: str,
        bids: Optional[list[tuple[float, float]]] = None,
        asks: Optional[list[tuple[float, float]]] = None,
        pip_size: float = 0.01,
    ) -> LiquiditySnapshot:
        """Analyze current liquidity for a symbol.

        Args:
            symbol: Trading symbol.
            bids: List of (price, volume) tuples for bids, sorted best-first.
            asks: List of (price, volume) tuples for asks, sorted best-first.
            pip_size: Pip size for the symbol (0.01 for metals, 0.1 for crypto).

        Returns:
            LiquiditySnapshot with full analysis.
        """
        if bids is None or len(bids) == 0 or asks is None or len(asks) == 0:
            return self._empty_snapshot(symbol)

        best_bid = bids[0][0]
        best_ask = asks[0][0]

        # Compute spread
        spread = best_ask - best_bid
        spread_pips = spread / pip_size if pip_size > 0 else spread
        avg_price = (best_bid + best_ask) / 2.0
        spread_pct = (spread / avg_price * 100) if avg_price > 0 else 0.0

        # Parse depth levels
        depth_levels = self._parse_depth_levels(bids, asks)

        # Compute total depth
        depth_bid_total = sum(b[1] for b in bids[:self._depth_levels])
        depth_ask_total = sum(a[1] for a in asks[:self._depth_levels])
        total_volume = depth_bid_total + depth_ask_total
        depth_imbalance = (
            (depth_bid_total - depth_ask_total) / total_volume
            if total_volume > 0
            else 0.0
        )

        # Determine liquidity level from spread
        liquidity_level = self._classify_liquidity(spread_pips)

        # Check for anomaly
        avg_spread = self._avg_spreads.get(symbol, spread_pips)
        spread_multiplier = spread_pips / avg_spread if avg_spread > 0 else 1.0
        is_anomalous = spread_multiplier >= self._anomaly_multiplier

        if is_anomalous:
            self._stats["anomalies"] += 1
            logger.warning(
                "Liquidity anomaly detected: %s spread=%.1f pips (%.1fx avg)",
                symbol, spread_pips, spread_multiplier,
            )

        # Update rolling average
        alpha = 0.3  # Smoothing factor
        self._avg_spreads[symbol] = alpha * spread_pips + (1 - alpha) * avg_spread

        snapshot = LiquiditySnapshot(
            symbol=symbol,
            bid=best_bid,
            ask=best_ask,
            spread=round(spread, 5),
            spread_pips=round(spread_pips, 2),
            spread_pct=round(spread_pct, 4),
            depth_bid_total=round(depth_bid_total, 2),
            depth_ask_total=round(depth_ask_total, 2),
            depth_imbalance=round(depth_imbalance, 4),
            depth_levels=depth_levels,
            liquidity_level=liquidity_level,
            is_anomalous=is_anomalous,
            spread_multiplier=round(spread_multiplier, 2),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        self._snapshots[symbol] = snapshot
        self._stats["snapshots"] += 1

        return snapshot

    def _parse_depth_levels(
        self, bids: list[tuple[float, float]], asks: list[tuple[float, float]]
    ) -> list[DepthLevel]:
        """Parse depth data into structured levels.

        Args:
            bids: List of (price, volume) tuples.
            asks: List of (price, volume) tuples.

        Returns:
            List of DepthLevel objects.
        """
        levels: list[DepthLevel] = []
        max_levels = min(self._depth_levels, max(len(bids), len(asks)))

        for i in range(max_levels):
            level = DepthLevel(level=i)
            if i < len(bids):
                level.bid_price = bids[i][0]
                level.bid_volume = bids[i][1]
            if i < len(asks):
                level.ask_price = asks[i][0]
                level.ask_volume = asks[i][1]
            levels.append(level)

        return levels

    def _classify_liquidity(self, spread_pips: float) -> str:
        """Classify liquidity level based on spread.

        Args:
            spread_pips: Spread in pips.

        Returns:
            Liquidity level string.
        """
        if spread_pips <= self._spread_max_pips * 0.5:
            return "abundant"
        elif spread_pips <= self._spread_max_pips:
            return "normal"
        elif spread_pips <= self._spread_max_pips * 2.5:
            return "thin"
        else:
            return "danger"

    def _empty_snapshot(self, symbol: str) -> LiquiditySnapshot:
        """Create an empty snapshot when no data is available.

        Args:
            symbol: Trading symbol.

        Returns:
            LiquiditySnapshot with default values.
        """
        return LiquiditySnapshot(
            symbol=symbol,
            liquidity_level="normal",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    # ── Risk & Confidence Adjustment ────────────

    def get_confidence_multiplier(self, symbol: str) -> float:
        """Get confidence multiplier based on current liquidity.

        Args:
            symbol: Trading symbol.

        Returns:
            Confidence multiplier (0.0-1.0).
        """
        snapshot = self._snapshots.get(symbol)
        if snapshot is None:
            return 1.0

        level_config = LIQUIDITY_LEVELS.get(snapshot.liquidity_level, LIQUIDITY_LEVELS["normal"])
        return level_config["confidence_mult"]

    def get_spread_filter(self, symbol: str) -> float:
        """Get max acceptable spread for signal filtering.

        Args:
            symbol: Trading symbol.

        Returns:
            Max spread in pips.
        """
        snapshot = self._snapshots.get(symbol)
        if snapshot is None:
            return self._spread_max_pips

        level_config = LIQUIDITY_LEVELS.get(snapshot.liquidity_level, LIQUIDITY_LEVELS["normal"])
        return self._spread_max_pips * level_config["max_spread_mult"]

    # ── Redis Caching ───────────────────────────

    async def cache_snapshot(self, snapshot: LiquiditySnapshot) -> bool:
        """Cache a liquidity snapshot to Redis with TTL.

        Args:
            snapshot: LiquiditySnapshot to cache.

        Returns:
            True if cached successfully.
        """
        if self._redis is None or not self._redis.is_initialized:
            return False

        try:
            # Store current snapshot with TTL
            cache_value = json.dumps({
                "symbol": snapshot.symbol,
                "bid": snapshot.bid,
                "ask": snapshot.ask,
                "spread": snapshot.spread,
                "spread_pips": snapshot.spread_pips,
                "spread_pct": snapshot.spread_pct,
                "depth_bid_total": snapshot.depth_bid_total,
                "depth_ask_total": snapshot.depth_ask_total,
                "depth_imbalance": snapshot.depth_imbalance,
                "liquidity_level": snapshot.liquidity_level,
                "is_anomalous": snapshot.is_anomalous,
                "spread_multiplier": snapshot.spread_multiplier,
                "timestamp": snapshot.timestamp,
            })
            await self._redis.set(
                REDIS_LIQUIDITY_CURRENT,
                cache_value,
                ex=self._cache_ttl,
            )
            self._stats["cache_writes"] += 1
            logger.debug("Liquidity snapshot cached: %s (level=%s)", snapshot.symbol, snapshot.liquidity_level)
            return True

        except Exception as exc:
            logger.error("Redis cache write failed for liquidity: %s", exc)
            self._stats["errors"] += 1
            return False

    async def cache_all_snapshots(self) -> bool:
        """Cache all current snapshots to Redis.

        Returns:
            True if at least one cached successfully.
        """
        if not self._snapshots:
            return False

        if self._redis is None or not self._redis.is_initialized:
            return False

        try:
            all_data = {
                symbol: {
                    "spread_pips": snap.spread_pips,
                    "liquidity_level": snap.liquidity_level,
                    "depth_imbalance": snap.depth_imbalance,
                    "is_anomalous": snap.is_anomalous,
                    "timestamp": snap.timestamp,
                }
                for symbol, snap in self._snapshots.items()
            }
            await self._redis.set(
                REDIS_LIQUIDITY_CURRENT,
                json.dumps(all_data),
                ex=self._cache_ttl,
            )
            self._stats["cache_writes"] += 1
            return True

        except Exception as exc:
            logger.error("Redis cache-all failed for liquidity: %s", exc)
            return False

    # ── Query ───────────────────────────────────

    async def get_current(self, symbol: str = "") -> Optional[dict]:
        """Get current liquidity data from Redis cache.

        Args:
            symbol: Trading symbol (or "" for all symbols).

        Returns:
            Dict with liquidity data, or None.
        """
        if self._redis is None or not self._redis.is_initialized:
            return self._snapshots.get(symbol).__dict__ if symbol in self._snapshots else None

        try:
            cached = await self._redis.get(REDIS_LIQUIDITY_CURRENT)
            if cached:
                data = json.loads(cached) if isinstance(cached, str) else json.loads(str(cached))
                if symbol and symbol in data:
                    return data[symbol]
                return data
        except Exception as exc:
            logger.warning("Redis get failed for liquidity: %s", exc)

        return None

    def get_latest_snapshot(self, symbol: str) -> Optional[LiquiditySnapshot]:
        """Get the most recent in-memory snapshot for a symbol.

        Args:
            symbol: Trading symbol.

        Returns:
            LiquiditySnapshot or None.
        """
        return self._snapshots.get(symbol)

    # ── Config ──────────────────────────────────

    async def load_config(self) -> None:
        """Load analyzer parameters from config_provider."""
        if self._config is None:
            return

        try:
            self._cache_ttl = await self._config.get_int(
                "liquidity_cache_ttl_sec", DEFAULT_CACHE_TTL_SEC
            )
            self._spread_max_pips = await self._config.get_float(
                "liquidity_spread_max_pips", DEFAULT_SPREAD_MAX_PIPS
            )
            logger.info("LiquidityAnalyzer config loaded: ttl=%ds, max_spread=%.1fpips",
                       self._cache_ttl, self._spread_max_pips)
        except Exception as exc:
            logger.warning("LiquidityAnalyzer config load failed: %s", exc)

    # ── Stats ───────────────────────────────────

    def get_stats(self) -> dict:
        """Get analyzer statistics."""
        return dict(self._stats)

    @property
    def active_symbols(self) -> list[str]:
        """List of symbols with cached snapshots."""
        return list(self._snapshots.keys())
