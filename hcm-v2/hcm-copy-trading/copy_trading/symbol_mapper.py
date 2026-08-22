"""Symbol Mapper — in-memory O(1) hash symbol mapping for copy trading.

Maps master account symbols to follower account symbols at startup
from PostgreSQL, with Redis PUB/SUB refresh for real-time updates.

Mapping modes:
- exact: literal match (e.g., XAUUSD → XAUUSD)
- prefix: prefix-based substitution (e.g., XAUUSD → XAUUSD.s)
- suffix: suffix-based substitution (e.g., XAUUSD → XAUUSD.pro)
- regex: regex pattern substitution (e.g., XAUUSD.* → GOLD)

Features:
- O(1) hash lookup from in-memory dict
- Redis PUB/SUB notification for cache refresh
- Case sensitivity configuration
- Suffix stripping
- Graceful fallback when mapping not found
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────

SYMBOL_REFRESH_CHANNEL = "hcm:symbol:refresh"
DEFAULT_MAPPING_MODE = "exact"


class MappingMode(str, Enum):
    """Symbol mapping modes."""
    EXACT = "exact"
    PREFIX = "prefix"
    SUFFIX = "suffix"
    REGEX = "regex"


@dataclass
class SymbolMapping:
    """A single symbol mapping entry."""
    master_symbol: str
    follower_symbol: str
    master_broker: str = ""
    follower_broker: str = ""
    mapping_mode: MappingMode = MappingMode.EXACT
    priority: int = 0
    strip_suffix: str = ""
    case_sensitive: bool = False


class SymbolMapper:
    """In-memory symbol mapper for copy trading.

    Loads symbol mappings from PostgreSQL at startup into a hash dict
    for O(1) lookup. Listens on Redis PUB/SUB channel "hcm:symbol:refresh"
    for real-time mapping updates.

    Supports four mapping modes with priority-based resolution:
    1. exact: direct symbol replacement
    2. prefix: prefix-based substitution
    3. suffix: suffix-based substitution
    4. regex: pattern-based replacement

    Example:
        mapper = SymbolMapper(db_pool, redis_client)
        await mapper.initialize()
        follower_symbol = await mapper.map_symbol("XAUUSD", 1, 6)
        # → "XAUUSD.pro" (if suffix mapping configured)
        await mapper.shutdown()
    """

    def __init__(
        self,
        db_pool: Any = None,
        redis_client: Any = None,
    ):
        """Initialize SymbolMapper.

        Args:
            db_pool: DatabasePool for loading symbol mappings.
            redis_client: RedisClient for PUB/SUB refresh notifications.
        """
        self._db = db_pool
        self._redis = redis_client

        # Primary hash: (master_account_id, master_symbol) → SymbolMapping
        self._mappings: dict[tuple, SymbolMapping] = {}

        # Regex mappings (evaluated in priority order when exact fails)
        self._regex_mappings: list[SymbolMapping] = []

        self._pubsub_task: Optional[asyncio.Task] = None
        self._initialized = False
        self._lock = asyncio.Lock()

    # ── Lifecycle ───────────────────────────────

    async def initialize(self) -> None:
        """Load symbol mappings from DB and start PUB/SUB listener."""
        if self._initialized:
            return

        await self._load_mappings()

        if self._redis is not None and self._redis.is_initialized:
            self._pubsub_task = asyncio.create_task(self._listen_refresh())

        self._initialized = True
        logger.info(
            "SymbolMapper initialized: %d mappings, %d regex patterns",
            len(self._mappings), len(self._regex_mappings),
        )

    async def shutdown(self) -> None:
        """Cancel PUB/SUB listener and clear caches."""
        if self._pubsub_task:
            self._pubsub_task.cancel()
            try:
                await self._pubsub_task
            except asyncio.CancelledError:
                pass
            self._pubsub_task = None
        self._mappings.clear()
        self._regex_mappings.clear()
        self._initialized = False
        logger.info("SymbolMapper shutdown complete")

    # ── Main API ────────────────────────────────

    async def map_symbol(
        self,
        symbol: str,
        master_account_id: int = 0,
        follower_account_id: int = 0,
    ) -> Optional[str]:
        """Map a master symbol to a follower symbol.

        Resolution order:
        1. Exact (master_account, symbol) key lookup → O(1)
        2. Regex patterns in priority order
        3. Fallback: same symbol (no mapping needed)

        Args:
            symbol: Master account's symbol.
            master_account_id: Master account ID.
            follower_account_id: Follower account ID.

        Returns:
            Mapped follower symbol, or None if no mapping exists.
        """
        # 1. Exact symbol lookup
        mapping = self._mappings.get(symbol)
        if mapping is not None:
            return self._apply_mapping(symbol, mapping)
        if mapping is not None:
            return self._apply_mapping(symbol, mapping)

        # 3. Try regex patterns
        for rm in self._regex_mappings:
            if rm.master_account_id not in (0, master_account_id):
                continue
            if rm.follower_account_id not in (0, follower_account_id):
                continue
            result = self._apply_regex_mapping(symbol, rm)
            if result is not None:
                return result

        # 4. No mapping — return None (caller should decide fallback)
        return None

    def _apply_mapping(self, symbol: str, mapping: SymbolMapping) -> str:
        """Apply a symbol mapping based on its mode.

        Args:
            symbol: Original symbol.
            mapping: SymbolMapping to apply.

        Returns:
            Mapped symbol string.
        """
        mode = mapping.mapping_mode
        follower = mapping.follower_symbol

        if mode == MappingMode.EXACT:
            # Exact = verbatim follower symbol. Strip is meaningless here and
            # would corrupt real names that end with the strip char (e.g. XAUUSD_).
            result = follower
        elif mode == MappingMode.PREFIX:
            result = follower + symbol
        elif mode == MappingMode.SUFFIX:
            result = symbol + follower
        else:
            result = follower  # regex handled separately

        # Strip suffix if configured — only for constructed modes; EXACT is verbatim.
        if mode != MappingMode.EXACT and mapping.strip_suffix and result.endswith(mapping.strip_suffix):
            result = result[:-len(mapping.strip_suffix)]

        # Case handling
        if not mapping.case_sensitive:
            result = result.upper()

        return result

    def _apply_regex_mapping(
        self, symbol: str, mapping: SymbolMapping
    ) -> Optional[str]:
        """Try to apply a regex-based mapping.

        Args:
            symbol: Original symbol.
            mapping: Regex SymbolMapping.

        Returns:
            Mapped symbol or None if no match.
        """
        try:
            flags = 0 if mapping.case_sensitive else re.IGNORECASE
            pattern = re.compile(mapping.master_symbol, flags)
            match = pattern.match(symbol)
            if match:
                result = pattern.sub(mapping.follower_symbol, symbol)
                if mapping.strip_suffix and result.endswith(mapping.strip_suffix):
                    result = result[:-len(mapping.strip_suffix)]
                return result.upper() if not mapping.case_sensitive else result
        except re.error as exc:
            logger.warning(
                "Invalid regex pattern '%s': %s", mapping.master_symbol, exc,
            )
        return None

    # ── Mapping Refresh ─────────────────────────

    async def refresh(self) -> None:
        """Force reload all symbol mappings from database."""
        async with self._lock:
            await self._load_mappings()
        logger.info("SymbolMapper mappings refreshed manually")

    async def _load_mappings(self) -> None:
        """Load all symbol mappings from PostgreSQL."""
        if self._db is None or not self._db.is_initialized:
            logger.warning("No DB available — symbol mappings empty")
            return

        try:
            rows = await self._db.fetch(
                "SELECT mapping_id, master_broker, master_symbol, "
                "follower_broker, follower_symbol, match_mode, match_priority, "
                "strip_suffixes, case_sensitive "
                "FROM hcm_copy.symbol_mappings WHERE is_active=true "
                "ORDER BY match_priority ASC"
            )

            new_mappings: dict[tuple, SymbolMapping] = {}
            new_regex: list[SymbolMapping] = []

            for row in rows:
                mapping_mode = MappingMode(row["match_mode"] or "exact")

                sm = SymbolMapping(
                    master_symbol=row["master_symbol"],
                    follower_symbol=row["follower_symbol"],
                    master_broker=row["master_broker"] or "",
                    follower_broker=row["follower_broker"] or "",
                    mapping_mode=mapping_mode,
                    priority=row["match_priority"] or 0,
                    strip_suffix=row["strip_suffixes"] or "",
                    case_sensitive=row["case_sensitive"] or False,
                )

                if mapping_mode == MappingMode.REGEX:
                    new_regex.append(sm)
                else:
                    key = sm.master_symbol
                    new_mappings[key] = sm

            self._mappings = new_mappings
            self._regex_mappings = new_regex
            logger.info(
                "Loaded %d exact + %d regex symbol mappings",
                len(self._mappings), len(self._regex_mappings),
            )
        except Exception as exc:
            logger.error("Failed to load symbol mappings: %s", exc)

    async def _listen_refresh(self) -> None:
        """Background task: listen for symbol mapping refresh on Redis PUB/SUB."""
        if self._redis is None:
            return

        pubsub = self._redis.pubsub()
        await pubsub.subscribe(SYMBOL_REFRESH_CHANNEL)
        logger.info("SymbolMapper subscribed to %s", SYMBOL_REFRESH_CHANNEL)

        try:
            async for message in pubsub.listen():
                if message["type"] == "message":
                    logger.info("Symbol mapping refresh notification received")
                    await self.refresh()
        except asyncio.CancelledError:
            await pubsub.unsubscribe(SYMBOL_REFRESH_CHANNEL)
            logger.info("SymbolMapper unsubscribed from %s", SYMBOL_REFRESH_CHANNEL)
        except Exception as exc:
            logger.error("SymbolMapper PUB/SUB listener error: %s", exc)

    # ── Stats & Health ──────────────────────────

    def get_stats(self) -> dict:
        """Get mapper statistics.

        Returns:
            Dict with mapping counts.
        """
        return {
            "exact_mappings": len(self._mappings),
            "regex_mappings": len(self._regex_mappings),
            "total_mappings": len(self._mappings) + len(self._regex_mappings),
        }

    async def health_check(self) -> dict:
        """Check mapper health.

        Returns:
            Dict with status and stats.
        """
        return {
            "status": "healthy" if self._initialized else "not_initialized",
            "initialized": self._initialized,
            "stats": self.get_stats(),
        }
