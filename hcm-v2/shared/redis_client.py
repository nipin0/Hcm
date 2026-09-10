"""Redis client wrapper — Stream + PUB/SUB + Hash operations.

Provides:
- Redis Stream: XADD / XREADGROUP / XACK / XGROUP CREATE
- PUB/SUB: publish / subscribe / unsubscribe
- Hash: hget / hset / hgetall
- Consumer Group management with ACK support
- Dead letter queue support
- Health check
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Union

import redis.asyncio as redis

logger = logging.getLogger(__name__)

# ── Redis Key Schemas ──────────────────────────
# Config:     hcm:config:v2 (Hash), hcm:config:version (Hash)
# Factor:     macro:latest:{category}, sentiment:latest:{category}
#             event:active, liquidity:current
# Streams:    signal:stream, signal:risk_passed, signal:dead
# PUB/SUB:    hcm:config:invalidate, hcm:event:warning,
#             hcm:event:circuit_breaker, hcm:symbol:refresh

DEFAULT_RETRY_MAX = 3
DEFAULT_RETRY_DELAY = 0.5
DEFAULT_STREAM_MAXLEN = 10000


@dataclass
class StreamMessage:
    """Parsed Redis Stream message."""
    message_id: str
    stream: str
    data: dict
    timestamp: float = field(default_factory=time.time)


class RedisClient:
    """Async Redis client with Stream, PUB/SUB, and Hash operations.

    Example usage:
        client = RedisClient("redis://localhost:6379")
        await client.initialize()

        # Stream
        msg_id = await client.xadd("signal:stream", {"event": "signal_created", ...})

        # PUB/SUB
        await client.publish("hcm:config:invalidate", "score_threshold")

        # Consumer Group
        messages = await client.xreadgroup("risk-engine-group", "risk-consumer",
                                            {"signal:stream": ">"}, count=10)
        await client.xack("signal:stream", "risk-engine-group", msg_id)

        await client.shutdown()
    """

    def __init__(
        self,
        url: str = "redis://localhost:6379",
        retry_max: int = DEFAULT_RETRY_MAX,
        retry_delay: float = DEFAULT_RETRY_DELAY,
    ):
        """Initialize RedisClient.

        Args:
            url: Redis connection URL.
            retry_max: Max retries for operations.
            retry_delay: Delay between retries (seconds).
        """
        self._url = url
        self._retry_max = retry_max
        self._retry_delay = retry_delay
        self._client: Optional[redis.Redis] = None
        self._initialized = False

    async def initialize(self) -> None:
        """Connect to Redis with retry."""
        if self._initialized:
            return

        for attempt in range(1, self._retry_max + 1):
            try:
                self._client = redis.from_url(
                    self._url,
                    decode_responses=True,
                    max_connections=20,
                )
                await self._client.ping()
                self._initialized = True
                logger.info("RedisClient connected: %s", self._url)
                return
            except Exception as exc:
                if attempt < self._retry_max:
                    wait = self._retry_delay * (2 ** (attempt - 1))
                    logger.warning(
                        "RedisClient connection attempt %d/%d failed: %s. Retrying in %.1fs...",
                        attempt, self._retry_max, exc, wait,
                    )
                    await asyncio.sleep(wait)

        raise RuntimeError(f"RedisClient: failed to connect after {self._retry_max} attempts")

    async def shutdown(self) -> None:
        """Gracefully close Redis connection."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            self._initialized = False
            logger.info("RedisClient shutdown complete")

    # ── Stream Operations ───────────────────────

    async def xadd(
        self,
        stream: str,
        data: dict[str, Any],
        maxlen: int = DEFAULT_STREAM_MAXLEN,
    ) -> Optional[str]:
        """Append a message to a Redis Stream.

        Args:
            stream: Stream key (e.g., "signal:stream").
            data: Message fields (dict values will be JSON-serialized).
            maxlen: Maximum stream length (oldest messages trimmed).

        Returns:
            Message ID string, or None if failed.
        """
        if self._client is None:
            raise RuntimeError("RedisClient not initialized")

        # Serialize non-string values
        serialized = {}
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                serialized[k] = json.dumps(v)
            elif not isinstance(v, str):
                serialized[k] = str(v)
            else:
                serialized[k] = v

        for attempt in range(1, self._retry_max + 1):
            try:
                msg_id = await self._client.xadd(stream, serialized, maxlen=maxlen)
                return msg_id
            except Exception as exc:
                logger.warning(
                    "Redis XADD to %s attempt %d/%d failed: %s",
                    stream, attempt, self._retry_max, exc,
                )
                if attempt < self._retry_max:
                    await asyncio.sleep(self._retry_delay * attempt)
        return None

    async def xreadgroup(
        self,
        group: str,
        consumer: str,
        streams: dict[str, str],
        count: int = 10,
        block: Optional[int] = 5000,
    ) -> list[StreamMessage]:
        """Read messages from streams as part of a consumer group.

        Args:
            group: Consumer group name.
            consumer: Consumer name within the group.
            streams: Dict of {stream_name: message_id} (use ">" for new messages).
            count: Max messages per stream.
            block: Block timeout in ms (None = non-blocking).

        Returns:
            List of parsed StreamMessage objects.
        """
        if self._client is None:
            raise RuntimeError("RedisClient not initialized")

        try:
            results = await self._client.xreadgroup(
                groupname=group,
                consumername=consumer,
                streams=streams,
                count=count,
                block=block,
            )
            if results is None:
                return []

            messages: list[StreamMessage] = []
            for stream_name, entries in results:
                for msg_id, fields in entries:
                    parsed_data: dict = {}
                    for k, v in fields.items():
                        try:
                            parsed_data[k] = json.loads(v)
                        except (json.JSONDecodeError, TypeError):
                            parsed_data[k] = v
                    messages.append(StreamMessage(
                        message_id=msg_id,
                        stream=stream_name,
                        data=parsed_data,
                    ))
            return messages
        except Exception as exc:
            # XREADGROUP with BLOCK=5000 times out every 5s when no new messages.
            # This is normal — downgrade to DEBUG to avoid log spam.
            if "Timeout" in str(exc) or "timeout" in str(exc).lower():
                logger.debug("Redis XREADGROUP timeout (expected when idle)")
            else:
                logger.error("Redis XREADGROUP failed: %s", exc)
            return []

    async def xack(self, stream: str, group: str, *message_ids: str) -> int:
        """Acknowledge processed messages in a consumer group.

        Args:
            stream: Stream key.
            group: Consumer group name.
            *message_ids: Message IDs to acknowledge.

        Returns:
            Number of acknowledged messages.
        """
        if self._client is None:
            raise RuntimeError("RedisClient not initialized")

        try:
            return await self._client.xack(stream, group, *message_ids)
        except Exception as exc:
            logger.error("Redis XACK on %s failed: %s", stream, exc)
            return 0

    async def xgroup_create(
        self,
        stream: str,
        group: str,
        mkstream: bool = True,
        start_id: str = "0",
    ) -> bool:
        """Create a consumer group (idempotent — ignores if already exists).

        WARNING: Using start_id="0" on a stream with old messages will
        REPLAY all historical messages on restart — catastrophic for
        order placement. Use "$" for non-recovery contexts.

        Args:
            stream: Stream key.
            group: Consumer group name.
            mkstream: Create stream if it doesn't exist.
            start_id: Position to start reading from (0 = beginning, $ = new only).

        Returns:
            True if created, False if already exists.
        """
        if self._client is None:
            raise RuntimeError("RedisClient not initialized")

        try:
            await self._client.xgroup_create(stream, group, id=start_id, mkstream=mkstream)
            logger.info("Created consumer group %s on stream %s", group, stream)
            return True
        except redis.ResponseError as exc:
            if "BUSYGROUP" in str(exc):
                logger.debug("Consumer group %s already exists on stream %s", group, stream)
                return False
            raise

    async def xpending(self, stream: str, group: str) -> int:
        """Get count of pending (unacknowledged) messages for a consumer group.

        Args:
            stream: Stream key.
            group: Consumer group name.

        Returns:
            Number of pending messages.
        """
        if self._client is None:
            raise RuntimeError("RedisClient not initialized")

        try:
            info = await self._client.xpending(stream, group)
            return info.get("pending", 0) if info else 0
        except Exception as exc:
            logger.error("Redis XPENDING on %s failed: %s", stream, exc)
            return 0

    async def xlen(self, stream: str) -> int:
        """Get the length of a stream.

        Args:
            stream: Stream key.

        Returns:
            Number of messages in the stream.
        """
        if self._client is None:
            raise RuntimeError("RedisClient not initialized")
        try:
            return await self._client.xlen(stream)
        except Exception as exc:
            logger.error("Redis XLEN on %s failed: %s", stream, exc)
            return 0

    # ── PUB/SUB ──────────────────────────────────

    async def publish(self, channel: str, message: str) -> int:
        """Publish a message to a Redis channel.

        Args:
            channel: Channel name.
            message: Message content.

        Returns:
            Number of subscribers that received the message.
        """
        if self._client is None:
            raise RuntimeError("RedisClient not initialized")

        try:
            return await self._client.publish(channel, message)
        except Exception as exc:
            logger.error("Redis PUBLISH to %s failed: %s", channel, exc)
            return 0

    def pubsub(self) -> redis.client.PubSub:
        """Get a PUB/SUB object for subscribing to channels.

        Returns:
            redis.asyncio.client.PubSub instance.
        """
        if self._client is None:
            raise RuntimeError("RedisClient not initialized")
        return self._client.pubsub()

    # ── Hash Operations ──────────────────────────

    async def hget(self, key: str, field: str) -> Optional[str]:
        """Get a field value from a Redis Hash.

        Args:
            key: Hash key.
            field: Field name.

        Returns:
            Field value or None.
        """
        if self._client is None:
            raise RuntimeError("RedisClient not initialized")

        try:
            return await self._client.hget(key, field)
        except Exception as exc:
            logger.error("Redis HGET %s[%s] failed: %s", key, field, exc)
            return None

    async def hset(self, key: str, field: str, value: str) -> int:
        """Set a field in a Redis Hash.

        Args:
            key: Hash key.
            field: Field name.
            value: Field value.

        Returns:
            1 if new field, 0 if updated.
        """
        if self._client is None:
            raise RuntimeError("RedisClient not initialized")

        try:
            return await self._client.hset(key, field, value)
        except Exception as exc:
            logger.error("Redis HSET %s[%s] failed: %s", key, field, exc)
            return -1

    async def hgetall(self, key: str) -> dict[str, str]:
        """Get all fields from a Redis Hash.

        Args:
            key: Hash key.

        Returns:
            Dict of field → value.
        """
        if self._client is None:
            raise RuntimeError("RedisClient not initialized")

        try:
            return await self._client.hgetall(key)
        except Exception as exc:
            logger.error("Redis HGETALL %s failed: %s", key, exc)
            return {}

    async def hdel(self, key: str, *fields: str) -> int:
        """Delete fields from a Redis Hash.

        Args:
            key: Hash key.
            *fields: Field names to delete.

        Returns:
            Number of deleted fields.
        """
        if self._client is None:
            raise RuntimeError("RedisClient not initialized")

        try:
            return await self._client.hdel(key, *fields)
        except Exception as exc:
            logger.error("Redis HDEL %s failed: %s", key, exc)
            return 0

    # ── Generic Operations ──────────────────────

    async def get(self, key: str) -> Optional[str]:
        """Get a key value."""
        if self._client is None:
            raise RuntimeError("RedisClient not initialized")
        try:
            return await self._client.get(key)
        except Exception as exc:
            logger.error("Redis GET %s failed: %s", key, exc)
            return None

    async def set(self, key: str, value: str, ex: Optional[int] = None) -> bool:
        """Set a key with optional TTL."""
        if self._client is None:
            raise RuntimeError("RedisClient not initialized")
        try:
            await self._client.set(key, value, ex=ex)
            return True
        except Exception as exc:
            logger.error("Redis SET %s failed: %s", key, exc)
            return False

    async def delete(self, *keys: str) -> int:
        """Delete keys."""
        if self._client is None:
            raise RuntimeError("RedisClient not initialized")
        try:
            return await self._client.delete(*keys)
        except Exception as exc:
            logger.error("Redis DELETE failed: %s", exc)
            return 0

    async def exists(self, key: str) -> bool:
        """Check whether a key exists. 【B7·2026-08-17 补】封装类缺该方法，
        ai_async_client.run_loop 用 redis_client.exists(_trigger) 检测 DeepSeek
        触发标志 → AttributeError 被静默吞 → run_loop 从不消费 trigger、从不调
        DeepSeek（AI 评分恒 src=lm_only）。"""
        if self._client is None:
            raise RuntimeError("RedisClient not initialized")
        try:
            n = await self._client.exists(key)
            return bool(n)
        except Exception as exc:
            logger.error("Redis EXISTS %s failed: %s", key, exc)
            return False

    def pipeline(self) -> redis.client.Pipeline:
        """Get a pipeline for batched operations."""
        if self._client is None:
            raise RuntimeError("RedisClient not initialized")
        return self._client.pipeline()

    async def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            if self._client is None:
                return False
            return await self._client.ping()
        except Exception:
            return False

    async def health_check(self) -> dict:
        """Check Redis health.

        Returns:
            Dict with status and info.
        """
        try:
            t0 = asyncio.get_event_loop().time()
            pong = await self._client.ping() if self._client else False
            latency = (asyncio.get_event_loop().time() - t0) * 1000
            info = await self._client.info("memory") if self._client else {}
            return {
                "status": "healthy" if pong else "unhealthy",
                "latency_ms": round(latency, 2),
                "used_memory_human": info.get("used_memory_human", "N/A"),
            }
        except Exception as exc:
            return {"status": "unhealthy", "error": str(exc)}

    @property
    def is_initialized(self) -> bool:
        """Check if client is initialized."""
        return self._initialized and self._client is not None

    @property
    def raw(self) -> redis.Redis:
        """Get the raw redis.asyncio.Redis client for advanced operations."""
        if self._client is None:
            raise RuntimeError("RedisClient not initialized")
        return self._client


# ────────────────────────────────────────────────────────────
# 2026-07-14: Safety Rail — Shared Config Validator
#
# Every service MUST call validate_safety_config() at startup.
# If any critical config value is empty or zero, the service
# refuses to start. This prevents the risk_cool_minutes=empty
# and max_open_positions=0 catastrophes.
# ────────────────────────────────────────────────────────────

CRITICAL_SAFETY_KEYS = {
    # ── 【2026-09-08 审计修复 P0】以下 4 键为历史遗留僵尸键：风控 rule_chain.load_config
    # 实际读的是 risk.max_lot_per_trade / risk.max_total_exposure / risk.max_concurrent_signals
    # / risk.cooldown_minutes（见下方新增），旧键被误配为 0 时自检"以为在保护"、
    # 真实阈值却完全裸奔。保留仅为兼容历史部署（缺失仍告警），真正的门禁见下方新键。
    "risk_cool_minutes":          {"type": "int",    "min":  0,  "max": 1440, "default": 5},
    "risk_max_open_positions":    {"type": "int",    "min":  1,  "max":   50, "default": 5},
    "risk_max_lot_single":        {"type": "float",  "min": 0.01, "max": 10.0, "default": 0.03},
    "risk_max_total_lot":         {"type": "float",  "min": 0.01, "max": 100.0,"default": 0.05},
    # ── 风控实际生效键（rule_chain.load_config 读取，务必与之一一对应）──
    # 注意：置信度键是**下划线** risk_min_confidence（下方"通用安全键"已含），
    # 不要写成 risk.min_confidence —— 该键在配置中心不存在，会导致启动自检
    # 判为缺失并中止服务启动（2026-09-08 实测事故，已即时修正）。
    "risk.max_lot_per_trade":     {"type": "float",  "min": 0.01, "max": 100.0,"default": 0.03},
    "risk.max_total_exposure":    {"type": "float",  "min": 0.01, "max": 10000.0,"default": 0.05},
    "risk.max_concurrent_signals":{"type": "int",   "min":  1,  "max":   50, "default": 10},
    "risk.max_daily_loss":        {"type": "float",  "min": 1.0,  "max": 100000.0,"default": 200},
    "risk.margin_call_level":     {"type": "float",  "min": 1.0,  "max": 100.0,"default": 20},
    "risk.cooldown_minutes":      {"type": "int",    "min":  0,  "max": 1440, "default": 5},
    # ── 通用安全键 ──
    "risk_min_confidence":        {"type": "float",  "min": 0.0,  "max": 1.0,  "default": 0.10},
    "close.trailing_stop_enabled":{"type": "bool",                      "default": True},
    "close.trailing_stop_distance":{"type": "int",  "min":  1,  "max":   10, "default": 2},
    "regime_adx_trend":           {"type": "int",   "min": 18,  "max":   40, "default": 24},
    "score_threshold":            {"type": "float", "min": 0.05, "max": 0.50, "default": 0.10},
    "datasource.timeframes":      {"type": "str",                      "default": "M5"},
}

import logging
logger = logging.getLogger("hcm.shared.redis_client")


def validate_safety_config(hcm_hash: dict[str, str | bytes]) -> tuple[bool, list[str]]:
    """Validate all safety-critical config keys at startup.

    Returns:
        (ok, errors) where errors is a list of human-readable failure messages.
        If ok=False, the service MUST abort startup.
    """
    errors: list[str] = []
    for key, spec in CRITICAL_SAFETY_KEYS.items():
        raw = hcm_hash.get(key)
        if raw is None or (isinstance(raw, bytes) and len(raw) == 0) or (isinstance(raw, str) and raw.strip() == ""):
            errors.append(f"{key}: CRITICAL — empty/missing (will default to {spec['default']})")
            continue
        try:
            if spec["type"] == "int":
                v = int(float(raw))
            elif spec["type"] == "float":
                v = float(raw)
            elif spec["type"] == "bool":
                v = str(raw).lower() in ("true", "1", "yes")
            else:
                v = str(raw)
            if spec.get("min") is not None and isinstance(v, (int, float)):
                if v < spec["min"]:
                    errors.append(f"{key}: {v} < min({spec['min']}) — too low, risk bypass")
            if spec.get("max") is not None and isinstance(v, (int, float)):
                if v > spec["max"]:
                    errors.append(f"{key}: {v} > max({spec['max']}) — too high, suspicious")
        except (ValueError, TypeError) as exc:
            errors.append(f"{key}: cannot parse '{raw}' — {exc}")
    return (len(errors) == 0, errors)
