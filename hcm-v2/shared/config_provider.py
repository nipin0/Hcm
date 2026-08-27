"""ConfigProviderV3 — Three-layer configuration with hot reload.

Layers (priority: L1 > L2 > L3):
  1. Local Memory  — dict cache with TTL + version check
  2. Redis         — Hash: hcm:config:v2 (key→value) + hcm:config:version (key→version_ts)
  3. PostgreSQL    — hcm_config.metadata (Source of Truth)

Invalidation: Redis PUB/SUB on channel "hcm:config:invalidate"

Config key resolution priority:
  1. symbol.{SYMBOL}.{key}     — symbol-level explicit override
  2. category.{CATEGORY}.{key} — category default
  3. {key}                     — global default
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Union

logger = logging.getLogger(__name__)

# ── Default local TTL ──────────────────────────
DEFAULT_LOCAL_TTL_SEC = 30
DEFAULT_REDIS_TTL_SEC = 300
INVALIDATION_CHANNEL = "hcm:config:invalidate"
CONFIG_HASH_KEY = "hcm:config:v2"
CONFIG_VERSION_KEY = "hcm:config:version"


@dataclass
class CacheEntry:
    """Local cache entry with version tracking."""
    value: str
    ts: float = field(default_factory=time.time)
    version: str = ""


class ConfigProviderV3:
    """Three-layer config provider with version-based invalidation.

    Supports per-symbol config override with the resolution chain:
    symbol.{SYMBOL}.{key} → category.{CATEGORY}.{key} → {key}

    Example usage:
        provider = ConfigProviderV3(pg_pool, redis_client)
        await provider.initialize()
        threshold = await provider.get_float("score_threshold", 0.50)
    """

    def __init__(
        self,
        pg_pool: Any = None,
        redis_client: Any = None,
        local_ttl: int = DEFAULT_LOCAL_TTL_SEC,
        redis_ttl: int = DEFAULT_REDIS_TTL_SEC,
    ):
        """Initialize ConfigProviderV3.

        Args:
            pg_pool: asyncpg connection pool (or None for Redis-only mode)
            redis_client: redis.asyncio.Redis instance (or None for PG-only mode)
            local_ttl: Local memory cache TTL in seconds
            redis_ttl: Redis cache TTL hint (for invalidation reference)
        """
        self._pg = pg_pool
        self._redis = redis_client
        self._local_ttl = local_ttl
        self._redis_ttl = redis_ttl
        self._cache: dict[str, CacheEntry] = {}
        self._pubsub_task: Optional[asyncio.Task] = None
        self._initialized = False

    # ── Initialization ──────────────────────────

    async def initialize(self) -> None:
        """Start the PUB/SUB listener for invalidation notifications."""
        if self._initialized:
            return
        if self._redis is not None:
            self._pubsub_task = asyncio.create_task(self._listen_invalidation())
        self._initialized = True
        logger.info("ConfigProviderV3 initialized (local_ttl=%ds)", self._local_ttl)

    async def shutdown(self) -> None:
        """Graceful shutdown: cancel PUB/SUB listener."""
        if self._pubsub_task:
            self._pubsub_task.cancel()
            try:
                await self._pubsub_task
            except asyncio.CancelledError:
                pass
        self._initialized = False

    # ── Public API ──────────────────────────────

    async def get(self, key: str, default: str = "") -> Optional[str]:
        """Get config value as raw string.

        Resolution chain: L1 (local) → L2 (Redis) → L3 (PostgreSQL).

        Args:
            key: Config key name.
            default: Fallback value if not found in any layer.

        Returns:
            Config value string, or default if not found.
        """
        # L1: Local memory cache
        cached = self._cache.get(key)
        if cached and (time.time() - cached.ts) < self._local_ttl:
            return cached.value

        # L2: Redis
        if self._redis is not None:
            try:
                redis_val = await self._redis.hget(CONFIG_HASH_KEY, key)
                redis_ver = await self._redis.hget(CONFIG_VERSION_KEY, key)
                if redis_val is not None:
                    val = redis_val.decode("utf-8") if isinstance(redis_val, bytes) else redis_val
                    ver = redis_ver.decode("utf-8") if isinstance(redis_ver, bytes) and redis_ver else ""
                    self._cache[key] = CacheEntry(value=val, version=ver)
                    return val
            except Exception as exc:
                logger.warning("Redis config read failed for key=%s: %s", key, exc)

        # L3: PostgreSQL (Source of Truth)
        if self._pg is not None:
            try:
                val = await self._load_from_pg(key)
                if val is not None:
                    return val
            except Exception as exc:
                logger.error("PostgreSQL config read failed for key=%s: %s", key, exc)

        # Not found in any layer
        if default:
            logger.debug("Config key=%s not found, using default=%s", key, default)
            return default
        return None

    async def get_current(self, key: str) -> Optional[str]:
        """Get ONLY the explicitly-set ``current_value`` (no ``default_value`` fallback).

        Unlike ``get()``, which returns ``COALESCE(current_value, default_value)``
        from PostgreSQL, this method returns ``None`` when the key has never been
        explicitly written (current_value IS NULL). This is essential for engines /
        panels that carry their own authoritative in-code default (e.g. hexp
        ``_DEFAULTS`` / web ``HEXP_KEYS``): a stale ``default_value`` seeded by an
        old migration must NOT masquerade as an effective runtime setting, otherwise
        a full-form save would flush that stale seed value back into ``current_value``
        and override the engine's intended default ("保存刷新又复原").

        Resolution chain: L1 (local) → L2 (Redis) → L3 (PostgreSQL current_value only).
        """
        # L1: Local memory cache
        cached = self._cache.get(key)
        if cached and (time.time() - cached.ts) < self._local_ttl:
            # 空串等同「未设置」（空串穿透防御，见 set/set_batch 空值归一）
            if cached.value == "":
                self._cache.pop(key, None)
                return None
            return cached.value

        # L2: Redis
        if self._redis is not None:
            try:
                redis_val = await self._redis.hget(CONFIG_HASH_KEY, key)
                redis_ver = await self._redis.hget(CONFIG_VERSION_KEY, key)
                if redis_val is not None:
                    val = redis_val.decode("utf-8") if isinstance(redis_val, bytes) else redis_val
                    # 空串视为未设置：不缓存、不返回（避免面板空白框 + 引擎静默回退默认）
                    if val == "":
                        self._cache.pop(key, None)
                        return None
                    ver = redis_ver.decode("utf-8") if isinstance(redis_ver, bytes) and redis_ver else ""
                    self._cache[key] = CacheEntry(value=val, version=ver)
                    return val
            except Exception as exc:
                logger.warning("Redis config read failed for key=%s: %s", key, exc)

        # L3: PostgreSQL (current_value only, NO default_value fallback)
        if self._pg is not None:
            try:
                async with self._pg.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT current_value FROM hcm_config.metadata WHERE config_key=$1",
                        key,
                    )
                if row is not None and row[0] is not None:
                    val = str(row[0])
                    # 空串视为未设置（防御历史数据残留空串穿透）
                    if val == "":
                        self._cache.pop(key, None)
                        return None
                    self._cache[key] = CacheEntry(value=val, version=str(time.time()))
                    return val
            except Exception as exc:
                logger.error("PostgreSQL current_value read failed for key=%s: %s", key, exc)

        return None

    async def get_with_resolution(
        self, key: str, symbol: str = "", category: str = "", default: str = ""
    ) -> Optional[str]:
        """Get config with symbol/category override resolution.

        Resolution chain:
          1. symbol.{SYMBOL}.{key}
          2. category.{CATEGORY}.{key}
          3. {key} (global)

        Args:
            key: Base config key (e.g., "inference_tf").
            symbol: Trading symbol (e.g., "XAUUSD").
            category: Symbol category (e.g., "metals").
            default: Fallback value.

        Returns:
            Resolved config value.
        """
        # 1. Symbol-level override
        if symbol:
            symbol_key = f"symbol.{symbol}.{key}"
            val = await self.get(symbol_key)
            if val is not None:
                return val

        # 2. Category-level default
        if category:
            cat_key = f"category.{category}.{key}"
            val = await self.get(cat_key)
            if val is not None:
                return val

        # 3. Global default
        return await self.get(key, default)

    async def get_int(self, key: str, default: int = 0) -> int:
        """Get config as int."""
        val = await self.get(key)
        if val is None:
            return default
        try:
            return int(val)
        except (ValueError, TypeError):
            logger.warning("Config key=%s value=%r is not int, using default=%d", key, val, default)
            return default

    async def get_float(self, key: str, default: float = 0.0) -> float:
        """Get config as float."""
        val = await self.get(key)
        if val is None:
            return default
        try:
            return float(val)
        except (ValueError, TypeError):
            logger.warning("Config key=%s value=%r is not float, using default=%f", key, val, default)
            return default

    async def get_bool(self, key: str, default: bool = False) -> bool:
        """Get config as bool."""
        val = await self.get(key)
        if val is None:
            return default
        return val.lower() in ("true", "1", "yes", "on")

    async def get_json(self, key: str, default: Any = None) -> Any:
        """Get config as parsed JSON."""
        val = await self.get(key)
        if val is None:
            return default
        try:
            return json.loads(val)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Config key=%s value is not valid JSON", key)
            return default

    async def get_keys_by_prefix(self, prefix: str) -> dict[str, str]:
        """Get all config keys matching ``prefix%`` from PostgreSQL (Source of Truth).

        Used for aggregate/discovery queries (e.g. all symbol-level tower configs,
        all watchdog service heartbeats). Keeps GET paths unified on the config
        provider instead of ad-hoc ``db_pool.fetch(LIKE ...)`` calls.

        Returns:
            Dict of {config_key: value} (value is "" when NULL).
        """
        if self._pg is None:
            logger.warning("Cannot read config by prefix: no PostgreSQL connection")
            return {}
        try:
            rows = await self._pg.fetch(
                "SELECT config_key, COALESCE(current_value, default_value) AS val "
                "FROM hcm_config.metadata WHERE config_key LIKE $1",
                f"{prefix}%",
            )
            return {
                row["config_key"]: (row["val"] if row["val"] is not None else "")
                for row in rows
            }
        except Exception as exc:
            logger.error("Config prefix read failed for prefix=%s: %s", prefix, exc)
            return {}

    async def set(self, key: str, value: str) -> bool:
        """Set config value (PG → Redis → PUB invalidate).

        Atomicity guarantee:
          - PostgreSQL (Source of Truth) is written first and MUST succeed.
          - Redis is L2 cache; its write failure does NOT fail the operation,
            but we proactively evict any stale cached value so subsequent
            ``get()`` calls fall through to PG (prevents split-brain where UI
            reads PG while engines read a stale Redis value).

        Returns:
            True if PG write succeeded.
        """
        if self._pg is None:
            logger.error("Cannot write config: no PostgreSQL connection")
            return False

        # 空值归一：空串 / 纯空白 / None 一律视为「未设置」。PG current_value 写 NULL、
        # Redis 删除该键，使 L2/L3 与「未显式设置」语义一致。彻底根治「空串穿透」缺陷
        # （空串被当成字面 current_value 落库 → 面板空白框 + 引擎静默回退默认）。
        # 这是铁律「全部配置参数必须双写」的核心修复（2026-08-22）。
        is_empty = (value is None) or (isinstance(value, str) and value.strip() == "")
        # 【2026-08-25 配置污染修复】写入前统一 strip，杜绝跨平台 CRLF 混入值尾
        # （如 hexp.mm.period 曾被存成 'M1\r' → time_frame 匹配不到数据 → MM 因子恒 0）。
        # 归一化后的 value 用于 PG 与 Redis 双写，保证两端一致（治本防 split-brain）。
        value = value.strip() if isinstance(value, str) and not is_empty else value
        pg_value = None if is_empty else str(value)

        # 1. Write to PG (Source of Truth) — must succeed
        try:
            async with self._pg.acquire() as conn:
                await conn.execute(
                    "INSERT INTO hcm_config.metadata (config_key, default_value, current_value, value_type, category) "
                    "VALUES ($1, $2, $3, 'string', 'scoring') "
                    "ON CONFLICT (config_key) DO UPDATE SET current_value=EXCLUDED.current_value, updated_at=now()",
                    key, "" if is_empty else str(value), pg_value,
                )
        except Exception as exc:
            logger.error("Config PG write failed for key=%s: %s", key, exc)
            return False

        # 2. Update Redis (L2 cache) — non-fatal, but evict stale value on failure
        if self._redis is not None:
            try:
                if is_empty:
                    # 空值 → 从 Redis 彻底删除，避免残留空串
                    async with self._redis.pipeline() as pipe:
                        pipe.hdel(CONFIG_HASH_KEY, key)
                        pipe.hdel(CONFIG_VERSION_KEY, key)
                        await pipe.execute()
                else:
                    version = str(time.time())
                    async with self._redis.pipeline() as pipe:
                        pipe.hset(CONFIG_HASH_KEY, key, str(value))
                        pipe.hset(CONFIG_VERSION_KEY, key, version)
                        await pipe.execute()
            except Exception as exc:
                logger.warning(
                    "Redis cache write failed for key=%s (PG is source of truth): %s",
                    key, exc,
                )
                # Evict any stale cached value so subsequent reads fall back to PG
                try:
                    await self._redis.hdel(CONFIG_HASH_KEY, key)
                    await self._redis.hdel(CONFIG_VERSION_KEY, key)
                except Exception:
                    pass

        # 3. Update local cache immediately
        if is_empty:
            self._cache.pop(key, None)
        else:
            self._cache[key] = CacheEntry(value=str(value), version=str(time.time()))

        # 4. Broadcast invalidation (non-fatal)
        if self._redis is not None:
            try:
                await self._redis.publish(INVALIDATION_CHANNEL, key)
            except Exception as exc:
                logger.warning("Config invalidation publish failed for key=%s: %s", key, exc)

        logger.info("Config key=%s updated to value=%s", key, ("<empty/None→NULL>" if is_empty else str(value)[:50]))
        return True

    async def set_batch(self, items: dict, category: str = "scoring") -> dict:
        """Batch upsert config values (PG → Redis → PUB invalidate).

        与 ``set()`` 相同的双写语义，但一次性写入多个键（供 P1b DeepSeek /
        Optuna 批量刷新 ``co.*`` 配置键，满足约束 ② PG+Redis 双写）。

        Args:
            items: ``{config_key: value}`` 映射；value 会被 ``str()`` 化。
            category: 新键（种子未覆盖时）的默认 category。

        Returns:
            ``{config_key: success_bool}``，逐键报告成败。
        """
        if self._pg is None:
            logger.error("Cannot write config batch: no PostgreSQL connection")
            return {k: False for k in items}

        norm: dict = {}
        empty_keys: set = set()
        for k, v in items.items():
            if v is None or (isinstance(v, str) and v.strip() == ""):
                empty_keys.add(str(k))
            else:
                norm[str(k)] = str(v)

        # 1. PG 批量 upsert（Source of Truth，必须成功）
        #    空值键：current_value 写 NULL（语义=未设置）；非空键：current_value=值。
        try:
            async with self._pg.acquire() as conn:
                entries = [(k, v, v, category) for k, v in norm.items()]
                for k in empty_keys:
                    entries.append((k, "", None, category))
                if entries:
                    await conn.executemany(
                        "INSERT INTO hcm_config.metadata "
                        "(config_key, default_value, current_value, value_type, category) "
                        "VALUES ($1, $2, $3, 'string', $4) "
                        "ON CONFLICT (config_key) DO UPDATE SET current_value=EXCLUDED.current_value, updated_at=now()",
                        entries,
                    )
        except Exception as exc:
            logger.error("Config PG batch write failed: %s", exc)
            return {k: False for k in items}

        # 2. Redis（L2 缓存，非致命；失败则逐键清理避免 split-brain）
        if self._redis is not None:
            try:
                version = str(time.time())
                async with self._redis.pipeline() as pipe:
                    for k, v in norm.items():
                        pipe.hset(CONFIG_HASH_KEY, k, v)
                        pipe.hset(CONFIG_VERSION_KEY, k, version)
                    for k in empty_keys:
                        pipe.hdel(CONFIG_HASH_KEY, k)
                        pipe.hdel(CONFIG_VERSION_KEY, k)
                    await pipe.execute()
            except Exception as exc:
                logger.warning(
                    "Redis batch cache write failed (PG is source of truth): %s", exc
                )
                try:
                    async with self._redis.pipeline() as pipe:
                        for k in norm:
                            pipe.hdel(CONFIG_HASH_KEY, k)
                            pipe.hdel(CONFIG_VERSION_KEY, k)
                        await pipe.execute()
                except Exception:
                    pass

        # 3. 本地缓存 + 失效广播
        result: dict = {}
        for k, v in norm.items():
            self._cache[k] = CacheEntry(value=v, version=str(time.time()))
            result[k] = True
        for k in empty_keys:
            self._cache.pop(k, None)
            result[k] = True
        if self._redis is not None:
            try:
                for k in norm:
                    await self._redis.publish(INVALIDATION_CHANNEL, k)
                for k in empty_keys:
                    await self._redis.publish(INVALIDATION_CHANNEL, k)
            except Exception as exc:
                logger.warning("Config batch invalidation publish failed: %s", exc)

        logger.info("Config batch updated %d keys (%d empty→NULL)", len(norm), len(empty_keys))
        return result

    async def invalidate_local(self, key: str) -> None:
        """Remove a key from local cache (called on invalidation notification)."""
        self._cache.pop(key, None)
        logger.debug("Local cache invalidated: key=%s", key)

    async def prefetch(self, keys: list[str]) -> None:
        """Pre-load a batch of keys into local cache."""
        for key in keys:
            await self.get(key)
        logger.info("Prefetched %d config keys", len(keys))

    # ── Internal ────────────────────────────────

    async def _load_from_pg(self, key: str) -> Optional[str]:
        """Load config value from PostgreSQL and backfill Redis."""
        async with self._pg.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COALESCE(current_value, default_value), updated_at "
                "FROM hcm_config.metadata WHERE config_key=$1",
                key,
            )
        if row is None:
            return None

        pg_val: str = row[0] if isinstance(row[0], str) else str(row[0])
        pg_ver: str = str(row[1].timestamp()) if row[1] else str(time.time())

        # Backfill Redis
        if self._redis is not None:
            try:
                async with self._redis.pipeline() as pipe:
                    pipe.hset(CONFIG_HASH_KEY, key, pg_val)
                    pipe.hset(CONFIG_VERSION_KEY, key, pg_ver)
                    await pipe.execute()
            except Exception as exc:
                logger.warning("Redis backfill failed for key=%s: %s", key, exc)

        self._cache[key] = CacheEntry(value=pg_val, version=pg_ver)
        return pg_val

    async def _listen_invalidation(self) -> None:
        """Background task: listen for invalidation PUB/SUB messages."""
        if self._redis is None:
            return
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(INVALIDATION_CHANNEL)
        logger.info("ConfigProviderV3: subscribed to %s", INVALIDATION_CHANNEL)
        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    key = message["data"]
                    if isinstance(key, bytes):
                        key = key.decode("utf-8")
                    await self.invalidate_local(key)
        except asyncio.CancelledError:
            await pubsub.unsubscribe(INVALIDATION_CHANNEL)
            logger.info("ConfigProviderV3: unsubscribed from %s", INVALIDATION_CHANNEL)
        except Exception as exc:
            logger.error("ConfigProviderV3 PUB/SUB listener error: %s", exc)
