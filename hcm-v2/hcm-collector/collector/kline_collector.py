"""K-line Collector — multi-symbol, multi-timeframe OHLCV collection.

Collects K-line data from hcm-gateway's gRPC StreamPrices feed,
aggregates into OHLCV candles, and writes to PostgreSQL + Redis cache.

Supports configurable symbols and timeframes via ConfigProviderV3.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────

DEFAULT_TIMEFRAMES = ["M1", "M5", "M15", "H1", "H4"]
DEFAULT_SYMBOLS = ["XAUUSD", "BTCUSD"]
BATCH_WRITE_SIZE = 100
KLINE_READY_TIMEOUT = 10.0  # seconds to wait for kline to be "ready"
COLLECT_INTERVAL = 1.0  # seconds between price fetches

# 2026-08-05 (D4): Redis 价源适配器。collector 当前无独立行情网关
# (hcm-gateway 为 STUB)，生产环境唯一实时价源是 mt5_bridge 写入 Redis
# 哈希 hcm:config:v2 的字段 market:latest:{symbol}（JSON: bid/ask/last）。
# 该适配器让 collector 复用桥已发布的实时价，从而在桥写入之外可选地
# 自启 K线循环（经 collector.kline_write_enabled 开关控制，默认关闭以避免
# 双写污染信号塔所依赖的 klines 数据）。
class RedisPriceSource:
    """Price source adapter that reads live prices from a Redis hash."""

    def __init__(self, redis_client: Any, hash_name: str = "hcm:config:v2"):
        self._redis = redis_client
        self._hash = hash_name

    async def get_current_price(self, symbol: str) -> Optional[dict]:
        if self._redis is None or not getattr(self._redis, "is_initialized", False):
            return None
        try:
            import json
            raw = await self._redis.hget(self._hash, f"market:latest:{symbol}")
            if not raw:
                return None
            data = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
            bid = float(data.get("bid", 0) or 0)
            ask = float(data.get("ask", 0) or 0)
            if bid <= 0 or ask <= 0:
                return None
            return {"bid": bid, "ask": ask, "volume": 0}
        except Exception as exc:
            logger.warning("RedisPriceSource fetch failed for %s: %s", symbol, exc)
            return None


@dataclass
class KlineCollectorConfig:
    """Configuration for a single symbol-timeframe collector."""
    symbol: str
    timeframe: str
    timeframe_seconds: int = 300  # M5 = 300s
    enabled: bool = True


@dataclass
class KlineBuffer:
    """In-progress K-line candle buffer."""
    symbol: str = ""
    timeframe: str = ""
    open_time: datetime = field(default_factory=datetime.utcnow)
    open: float = 0.0
    high: float = float("-inf")
    low: float = float("inf")
    close: float = 0.0
    tick_volume: int = 0
    spread: float = 0.0  # [2026-07-30 A 组] 本棒期间最大点差（ask-bid），仅当数据源提供
    tick_count: int = 0
    closed: bool = False

    @property
    def is_initialized(self) -> bool:
        """Whether the buffer has received at least one tick."""
        return self.tick_count > 0

    def update(self, bid: float, ask: float, volume: int = 0) -> None:
        """Update the candle with a new price tick.

        Args:
            bid: Current bid price.
            ask: Current ask price.
            volume: Tick volume.
        """
        mid = (bid + ask) / 2.0
        if not self.is_initialized:
            self.open = mid
        self.high = max(self.high, mid)
        self.low = min(self.low, mid)
        self.close = mid
        self.tick_volume += volume
        # 点差取本棒期间观测到的最大 ask-bid（更贴近“最差成交成本”）
        self.spread = max(self.spread, (ask - bid))
        self.tick_count += 1

    def to_dict(self) -> dict:
        """Serialize candle to dict for DB/Redis."""
        return {
            "symbol": self.symbol,
            "time_frame": self.timeframe,
            "open_time": self.open_time.isoformat(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "tick_volume": self.tick_volume,
            "spread": self.spread,
            "tick_count": self.tick_count,
        }

    def reset(self, new_open_time: datetime) -> None:
        """Reset buffer for a new candle period.

        Args:
            new_open_time: Open time for the new candle.
        """
        self.open_time = new_open_time
        self.open = 0.0
        self.high = float("-inf")
        self.low = float("inf")
        self.close = 0.0
        self.tick_volume = 0
        self.tick_count = 0
        self.closed = False


class KlineCollector:
    """Multi-symbol, multi-timeframe K-line collector.

    Pulls real-time prices from price_source (gRPC/WS/Mt5Bridge),
    aggregates into OHLCV candles, and persists to PostgreSQL and Redis.

    Example:
        collector = KlineCollector(db_pool, redis_client, config_provider, price_source)
        await collector.start()
        # ... collection runs in background ...
        klines = await collector.get_latest_klines("XAUUSD", "M5", 100)
        await collector.stop()
    """

    # Timeframe → seconds mapping
    TIMEFRAME_SECONDS: dict[str, int] = {
        "M1": 60,
        "M5": 300,
        "M15": 900,
        "M30": 1800,
        "H1": 3600,
        "H4": 14400,
        "D1": 86400,
    }

    def __init__(
        self,
        db_pool: Any = None,
        redis_client: Any = None,
        config_provider: Any = None,
        price_source: Any = None,
        pg_write_enabled: bool = True,
    ):
        """Initialize KlineCollector.

        Args:
            db_pool: DatabasePool instance for PostgreSQL writes.
            redis_client: RedisClient instance for cache writes.
            config_provider: ConfigProviderV3 for runtime configuration.
            price_source: Callable or async object for fetching prices (grpc_server, ws, or mt5_bridge).
            pg_write_enabled: 是否将聚合后的 K线写入 PostgreSQL。生产环境默认由
                mt5_bridge 独家写入 klines；collector 复用桥的 Redis 价源自启循环时
                应设为 False，避免同一张表双写污染信号塔所依赖的 OHLC 数据
                （2026-08-05 D4）。
        """
        self._db = db_pool
        self._redis = redis_client
        self._config = config_provider
        self._price_source = price_source
        self._pg_write_enabled = pg_write_enabled

        self._buffers: dict[tuple[str, str], KlineBuffer] = {}  # (symbol, tf) → buffer
        self._ready_klines: asyncio.Queue = asyncio.Queue(maxsize=BATCH_WRITE_SIZE * 2)

        self._running = False
        self._collect_task: Optional[asyncio.Task] = None
        self._write_task: Optional[asyncio.Task] = None
        self._stats: dict[str, int] = {
            "klines_collected": 0,
            "klines_written_pg": 0,
            "klines_written_redis": 0,
            "errors": 0,
        }

    # ── Lifecycle ───────────────────────────────

    async def start(
        self,
        symbols: Optional[list[str]] = None,
        timeframes: Optional[list[str]] = None,
    ) -> None:
        """Start K-line collection.

        Args:
            symbols: List of symbols to collect (default: from config or ["XAUUSD", "BTCUSD"]).
            timeframes: List of timeframes (default: from config or ["M1","M5","M15","H1","H4"]).
        """
        if symbols is None:
            if self._config is not None:
                active_json = await self._config.get_json("active_symbols", DEFAULT_SYMBOLS)
                symbols = active_json if isinstance(active_json, list) else DEFAULT_SYMBOLS
            else:
                symbols = DEFAULT_SYMBOLS

        if timeframes is None:
            if self._config is not None:
                tf_json = await self._config.get_json("collector_timeframes", DEFAULT_TIMEFRAMES)
                timeframes = tf_json if isinstance(tf_json, list) else DEFAULT_TIMEFRAMES
            else:
                timeframes = DEFAULT_TIMEFRAMES

        # Initialize buffers for all symbol-tf pairs
        now = datetime.utcnow()
        for symbol in symbols:
            for tf in timeframes:
                tf_sec = self.TIMEFRAME_SECONDS.get(tf, 300)
                # Align to timeframe boundary
                epoch = int(now.timestamp())
                aligned_epoch = (epoch // tf_sec) * tf_sec
                open_time = datetime.utcfromtimestamp(aligned_epoch)
                key = (symbol, tf)
                self._buffers[key] = KlineBuffer(
                    symbol=symbol,
                    timeframe=tf,
                    open_time=open_time,
                )

        self._running = True
        self._collect_task = asyncio.create_task(self._collection_loop(symbols, timeframes))
        self._write_task = asyncio.create_task(self._write_loop())

        logger.info(
            "KlineCollector started: symbols=%s, timeframes=%s, buffers=%d",
            symbols, timeframes, len(self._buffers),
        )

    async def stop(self) -> None:
        """Gracefully stop K-line collection."""
        self._running = False

        for task in [self._collect_task, self._write_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        logger.info("KlineCollector stopped (stats=%s)", self._stats)

    # ── Collection Loop ─────────────────────────

    async def _collection_loop(
        self, symbols: list[str], timeframes: list[str]
    ) -> None:
        """Main collection loop: fetch prices, aggregate into candles."""
        logger.info("Kline collection loop started")
        current_minute = -1

        try:
            while self._running:
                now = datetime.utcnow()

                # Fetch prices for all symbols
                for symbol in symbols:
                    tick = await self._fetch_price(symbol)
                    if tick is None:
                        continue

                    bid = tick.bid if hasattr(tick, 'bid') else tick.get('bid', 0)
                    ask = tick.ask if hasattr(tick, 'ask') else tick.get('ask', 0)
                    volume = tick.volume if hasattr(tick, 'volume') else tick.get('volume', 0)

                    # Update each timeframe buffer
                    for tf in timeframes:
                        key = (symbol, tf)
                        buffer = self._buffers.get(key)
                        if buffer is None:
                            continue

                        tf_sec = self.TIMEFRAME_SECONDS.get(tf, 300)
                        epoch = int(now.timestamp())
                        aligned_epoch = (epoch // tf_sec) * tf_sec
                        expected_open = datetime.utcfromtimestamp(aligned_epoch)

                        # Check if candle period has changed
                        if buffer.open_time < expected_open:
                            # Close current candle
                            if buffer.is_initialized:
                                buffer.closed = True
                                closed_candle = buffer.to_dict()
                                await self._ready_klines.put(closed_candle)
                                self._stats["klines_collected"] += 1

                            # Start new candle
                            buffer.reset(expected_open)

                        # Update candle OHLCV
                        buffer.update(bid, ask, volume)

                await asyncio.sleep(COLLECT_INTERVAL)

        except asyncio.CancelledError:
            logger.info("Kline collection loop stopped")

    # ── Write Loop ──────────────────────────────

    async def _write_loop(self) -> None:
        """Background task: write closed klines to PG + Redis."""
        logger.info("Kline write loop started")
        batch: list[dict] = []

        try:
            while self._running:
                try:
                    kline = await asyncio.wait_for(
                        self._ready_klines.get(), timeout=5.0
                    )
                    batch.append(kline)

                    if len(batch) >= BATCH_WRITE_SIZE:
                        await self._persist_batch(batch)
                        batch.clear()
                except asyncio.TimeoutError:
                    if batch:
                        await self._persist_batch(batch)
                        batch.clear()
        except asyncio.CancelledError:
            # Flush remaining on cancel
            if batch:
                await self._persist_batch(batch)
            logger.info("Kline write loop stopped")

    async def _persist_batch(self, batch: list[dict]) -> None:
        """Write a batch of closed candles to PG and Redis.

        Args:
            batch: List of candle dicts to persist.
        """
        if not batch:
            return

        # Write to PostgreSQL
        if self._pg_write_enabled and self._db is not None and self._db.is_initialized:
            try:
                for kline in batch:
                    await self._db.execute(
                        """INSERT INTO hcm_market.klines
                           (symbol, time_frame, open_time, open, high, low, close, tick_volume, spread, source)
                           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                           ON CONFLICT (symbol, time_frame, open_time) DO UPDATE SET
                           high = GREATEST(hcm_market.klines.high, $5),
                           low = LEAST(hcm_market.klines.low, $6),
                           close = $7,
                           tick_volume = hcm_market.klines.tick_volume + $8,
                           spread = GREATEST(hcm_market.klines.spread, $9)""",
                        kline["symbol"],
                        kline["time_frame"],
                        kline["open_time"],
                        kline["open"],
                        kline["high"],
                        kline["low"],
                        kline["close"],
                        kline["tick_volume"],
                        kline.get("spread", 0.0),
                        "collector",
                    )
                self._stats["klines_written_pg"] += len(batch)
            except Exception as exc:
                logger.error("Kline PG write failed: %s", exc)
                self._stats["errors"] += 1

        # Write to Redis cache
        if self._redis is not None and self._redis.is_initialized:
            try:
                import json
                for kline in batch:
                    key = f"latest_kline:{kline['symbol']}:{kline['time_frame']}"
                    await self._redis.set(key, json.dumps(kline), ex=3600)
                self._stats["klines_written_redis"] += len(batch)
            except Exception as exc:
                logger.error("Kline Redis write failed: %s", exc)

    # ── Price Fetching ──────────────────────────

    async def _fetch_price(self, symbol: str) -> Optional[Any]:
        """Fetch current price for a symbol from the price source.

        Args:
            symbol: Trading symbol.

        Returns:
            Price tick object or None.
        """
        if self._price_source is None:
            return None

        try:
            if hasattr(self._price_source, 'get_current_price'):
                return await self._price_source.get_current_price(symbol)
            elif asyncio.iscoroutinefunction(self._price_source):
                return await self._price_source(symbol)
            elif callable(self._price_source):
                return self._price_source(symbol)
        except Exception as exc:
            logger.warning("Price fetch failed for %s: %s", symbol, exc)

        return None

    # ── Query ───────────────────────────────────

    async def get_latest_klines(
        self, symbol: str, timeframe: str, limit: int = 100
    ) -> list[dict]:
        """Get latest klines from Redis cache or PostgreSQL.

        Args:
            symbol: Trading symbol.
            timeframe: Timeframe string (e.g., "M5").
            limit: Maximum number of klines to return.

        Returns:
            List of kline dicts sorted by open_time descending.
        """
        # Try Redis first
        if self._redis is not None and self._redis.is_initialized:
            try:
                key = f"latest_kline:{symbol}:{timeframe}"
                cached = await self._redis.get(key)
                if cached:
                    import json
                    return [json.loads(cached)]
            except Exception:
                pass

        # Fall back to PG
        if self._db is not None and self._db.is_initialized:
            try:
                rows = await self._db.fetch(
                    """SELECT symbol, time_frame, open_time, open, high, low, close, tick_volume
                       FROM hcm_market.klines
                       WHERE symbol=$1 AND time_frame=$2
                       ORDER BY open_time DESC LIMIT $3""",
                    symbol, timeframe, limit,
                )
                return [
                    {
                        "symbol": r["symbol"],
                        "time_frame": r["time_frame"],
                        "open_time": r["open_time"].isoformat(),
                        "open": float(r["open"]),
                        "high": float(r["high"]),
                        "low": float(r["low"]),
                        "close": float(r["close"]),
                        "tick_volume": int(r["tick_volume"]),
                    }
                    for r in rows
                ]
            except Exception as exc:
                logger.warning("Kline query from PG failed: %s", exc)

        return []

    async def get_current_buffer(
        self, symbol: str, timeframe: str
    ) -> Optional[dict]:
        """Get the current in-progress candle buffer.

        Args:
            symbol: Trading symbol.
            timeframe: Timeframe string.

        Returns:
            Current candle dict or None.
        """
        key = (symbol, timeframe)
        buffer = self._buffers.get(key)
        if buffer and buffer.is_initialized:
            return buffer.to_dict()
        return None

    # ── Stats & Health ──────────────────────────

    async def get_stats(self) -> dict:
        """Get collection statistics.

        Returns:
            Dict with collection stats.
        """
        return {
            **self._stats,
            "active_buffers": len(self._buffers),
            "ready_queue_size": self._ready_klines.qsize(),
        }

    async def health_check(self) -> dict:
        """Check collector health.

        Returns:
            Dict with status and stats.
        """
        return {
            "status": "healthy" if self._running else "stopped",
            "stats": await self.get_stats(),
        }
