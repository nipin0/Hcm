"""Tick Collector — real-time bid/ask/spread collection.

Collects tick-level price data for all active symbols and writes
to PostgreSQL (hcm_market.ticks partition) and Redis cache.

Ticks provide the highest resolution price data for:
- Spread monitoring and liquidity analysis
- Slippage estimation
- Short-term volatility measurement
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────

DEFAULT_SYMBOLS = ["XAUUSD", "BTCUSD"]
TICK_COLLECT_INTERVAL = 0.2  # seconds (5 ticks/sec)
BATCH_WRITE_SIZE = 500
TICK_REDIS_TTL = 5  # seconds — ticks are short-lived cache
TICK_STALE_THRESHOLD = 10.0  # seconds — warn if no tick data


@dataclass
class TickRecord:
    """A single tick data point."""
    symbol: str = ""
    bid: float = 0.0
    ask: float = 0.0
    spread: float = 0.0
    volume: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        """Serialize tick to dict."""
        return {
            "symbol": self.symbol,
            "bid": self.bid,
            "ask": self.ask,
            "spread": self.spread,
            "volume": self.volume,
            "timestamp": datetime.utcfromtimestamp(self.timestamp).isoformat(),
        }

    def to_redis_dict(self) -> dict:
        """Serialize tick for Redis cache (compact)."""
        return {
            "symbol": self.symbol,
            "bid": str(self.bid),
            "ask": str(self.ask),
            "spread": str(self.spread),
            "ts": str(int(self.timestamp * 1000)),
            "vol": str(self.volume),
        }


class TickCollector:
    """Real-time tick data collector.

    Collects bid/ask/spread at high frequency, batches writes to
    PostgreSQL and maintains a short-lived Redis cache for the
    latest tick per symbol.

    Example:
        collector = TickCollector(db_pool, redis_client, config_provider, price_source)
        await collector.start()
        tick = await collector.get_latest_tick("XAUUSD")
        await collector.stop()
    """

    def __init__(
        self,
        db_pool: Any = None,
        redis_client: Any = None,
        config_provider: Any = None,
        price_source: Any = None,
    ):
        """Initialize TickCollector.

        Args:
            db_pool: DatabasePool instance for PostgreSQL writes.
            redis_client: RedisClient instance for cache.
            config_provider: ConfigProviderV3 for runtime configuration.
            price_source: Price source for fetching current bid/ask.
        """
        self._db = db_pool
        self._redis = redis_client
        self._config = config_provider
        self._price_source = price_source

        self._tick_queue: asyncio.Queue = asyncio.Queue(maxsize=BATCH_WRITE_SIZE * 3)
        self._latest_ticks: dict[str, TickRecord] = {}

        self._running = False
        self._collect_task: Optional[asyncio.Task] = None
        self._write_task: Optional[asyncio.Task] = None
        self._redis_task: Optional[asyncio.Task] = None

        self._stats: dict[str, int] = {
            "ticks_collected": 0,
            "ticks_written_pg": 0,
            "ticks_written_redis": 0,
            "errors": 0,
        }

    # ── Lifecycle ───────────────────────────────

    async def start(self, symbols: Optional[list[str]] = None) -> None:
        """Start tick collection.

        Args:
            symbols: List of symbols to collect ticks for.
        """
        if symbols is None:
            if self._config is not None:
                active_json = await self._config.get_json("active_symbols", DEFAULT_SYMBOLS)
                symbols = active_json if isinstance(active_json, list) else DEFAULT_SYMBOLS
            else:
                symbols = DEFAULT_SYMBOLS

        self._running = True
        self._collect_task = asyncio.create_task(self._collection_loop(symbols))
        self._write_task = asyncio.create_task(self._write_loop())
        self._redis_task = asyncio.create_task(self._redis_cache_loop(symbols))

        logger.info("TickCollector started: symbols=%s", symbols)

    async def stop(self) -> None:
        """Gracefully stop tick collection."""
        self._running = False

        for task in [self._collect_task, self._write_task, self._redis_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        logger.info("TickCollector stopped (stats=%s)", self._stats)

    # ── Collection Loop ─────────────────────────

    async def _collection_loop(self, symbols: list[str]) -> None:
        """Main tick collection loop."""
        logger.info("Tick collection loop started (interval=%.1fs)", TICK_COLLECT_INTERVAL)

        try:
            while self._running:
                for symbol in symbols:
                    tick = await self._fetch_tick(symbol)
                    if tick is None:
                        continue

                    record = TickRecord(
                        symbol=symbol,
                        bid=tick.bid if hasattr(tick, 'bid') else tick.get('bid', 0),
                        ask=tick.ask if hasattr(tick, 'ask') else tick.get('ask', 0),
                        spread=tick.spread if hasattr(tick, 'spread') else (
                            (tick.get('ask', 0) - tick.get('bid', 0)) if isinstance(tick, dict) else 0
                        ),
                        volume=tick.volume if hasattr(tick, 'volume') else tick.get('volume', 0),
                    )

                    # Update latest tick cache
                    self._latest_ticks[symbol] = record

                    # Queue for batch write
                    if not self._tick_queue.full():
                        await self._tick_queue.put(record)
                        self._stats["ticks_collected"] += 1

                await asyncio.sleep(TICK_COLLECT_INTERVAL)

        except asyncio.CancelledError:
            logger.info("Tick collection loop stopped")

    # ── Write Loop ──────────────────────────────

    async def _write_loop(self) -> None:
        """Background task: batch write ticks to PostgreSQL."""
        logger.info("Tick write loop started")
        batch: list[TickRecord] = []

        try:
            while self._running:
                try:
                    tick = await asyncio.wait_for(self._tick_queue.get(), timeout=5.0)
                    batch.append(tick)

                    if len(batch) >= BATCH_WRITE_SIZE:
                        await self._persist_batch(batch)
                        batch.clear()
                except asyncio.TimeoutError:
                    if batch:
                        await self._persist_batch(batch)
                        batch.clear()
        except asyncio.CancelledError:
            if batch:
                await self._persist_batch(batch)
            logger.info("Tick write loop stopped")

    async def _persist_batch(self, batch: list[TickRecord]) -> None:
        """Write a batch of ticks to PostgreSQL.

        Args:
            batch: List of TickRecord objects.
        """
        if not batch or self._db is None or not self._db.is_initialized:
            return

        try:
            # Use COPY-like batch insert for performance
            values = [
                (t.symbol, datetime.utcfromtimestamp(t.timestamp),
                 t.bid, t.ask, t.spread, t.volume)
                for t in batch
            ]
            async with self._db.acquire() as conn:
                await conn.executemany(
                    """INSERT INTO hcm_market.ticks 
                       (symbol, timestamp, bid, ask, spread, volume)
                       VALUES ($1, $2, $3, $4, $5, $6)""",
                    values,
                )
            self._stats["ticks_written_pg"] += len(batch)
        except Exception as exc:
            logger.error("Tick PG write failed: %s (batch_size=%d)", exc, len(batch))
            self._stats["errors"] += 1

    # ── Redis Cache Loop ────────────────────────

    async def _redis_cache_loop(self, symbols: list[str]) -> None:
        """Background task: periodically update Redis with latest ticks."""
        logger.info("Tick Redis cache loop started")

        try:
            while self._running:
                if self._redis is not None and self._redis.is_initialized:
                    for symbol in symbols:
                        tick = self._latest_ticks.get(symbol)
                        if tick is None:
                            continue
                        try:
                            key = f"tick:latest:{symbol}"
                            await self._redis.set(
                                key,
                                json.dumps(tick.to_redis_dict()),
                                ex=TICK_REDIS_TTL,
                            )
                            self._stats["ticks_written_redis"] += 1
                        except Exception as exc:
                            logger.debug("Tick Redis write for %s failed: %s", symbol, exc)

                await asyncio.sleep(TICK_REDIS_TTL)

        except asyncio.CancelledError:
            logger.info("Tick Redis cache loop stopped")

    # ── Price Fetching ──────────────────────────

    async def _fetch_tick(self, symbol: str) -> Optional[Any]:
        """Fetch current tick for a symbol.

        Args:
            symbol: Trading symbol.

        Returns:
            Tick data or None.
        """
        if self._price_source is None:
            return None

        try:
            if hasattr(self._price_source, 'get_current_price'):
                return await self._price_source.get_current_price(symbol)
            elif callable(self._price_source):
                return self._price_source(symbol) if not asyncio.iscoroutinefunction(self._price_source) else await self._price_source(symbol)
        except Exception as exc:
            logger.debug("Tick fetch failed for %s: %s", symbol, exc)

        return None

    # ── Query ───────────────────────────────────

    async def get_latest_tick(self, symbol: str) -> Optional[TickRecord]:
        """Get the most recent tick for a symbol (in-memory).

        Args:
            symbol: Trading symbol.

        Returns:
            Latest TickRecord or None.
        """
        return self._latest_ticks.get(symbol)

    async def get_latest_tick_from_redis(self, symbol: str) -> Optional[dict]:
        """Get latest tick from Redis cache.

        Args:
            symbol: Trading symbol.

        Returns:
            Tick dict or None.
        """
        if self._redis is None or not self._redis.is_initialized:
            return None

        try:
            key = f"tick:latest:{symbol}"
            data = await self._redis.get(key)
            if data:
                return json.loads(data)
        except Exception:
            pass

        return None

    async def get_recent_ticks(
        self, symbol: str, limit: int = 100
    ) -> list[dict]:
        """Get recent ticks from PostgreSQL.

        Args:
            symbol: Trading symbol.
            limit: Max number of ticks to return.

        Returns:
            List of tick dicts.
        """
        if self._db is None or not self._db.is_initialized:
            return []

        try:
            rows = await self._db.fetch(
                """SELECT symbol, timestamp, bid, ask, spread, volume
                   FROM hcm_market.ticks
                   WHERE symbol=$1
                   ORDER BY timestamp DESC LIMIT $2""",
                symbol, limit,
            )
            return [
                {
                    "symbol": r["symbol"],
                    "timestamp": r["timestamp"].isoformat(),
                    "bid": float(r["bid"]),
                    "ask": float(r["ask"]),
                    "spread": float(r["spread"]),
                    "volume": int(r["volume"]),
                }
                for r in rows
            ]
        except Exception as exc:
            logger.warning("Tick query from PG failed: %s", exc)
            return []

    # ── Stats & Health ──────────────────────────

    async def get_stats(self) -> dict:
        """Get collection statistics.

        Returns:
            Dict with collection stats.
        """
        return {
            **self._stats,
            "latest_ticks_count": len(self._latest_ticks),
            "queue_size": self._tick_queue.qsize(),
        }

    async def health_check(self) -> dict:
        """Check collector health.

        Returns:
            Dict with status and stats.
        """
        # Check tick freshness
        stale_symbols = []
        now = time.time()
        for symbol, tick in self._latest_ticks.items():
            if now - tick.timestamp > TICK_STALE_THRESHOLD:
                stale_symbols.append(symbol)

        status = "healthy" if self._running and not stale_symbols else "degraded"

        return {
            "status": status,
            "stats": await self.get_stats(),
            "stale_symbols": stale_symbols if stale_symbols else None,
        }
