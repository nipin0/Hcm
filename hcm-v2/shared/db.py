"""Async PostgreSQL connection pool management.

Provides a singleton-style connection pool via asyncpg with:
- Automatic reconnect on connection loss
- Connection retry with exponential backoff
- Graceful shutdown
- Query timeout support
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

import asyncpg

logger = logging.getLogger(__name__)

DEFAULT_MIN_SIZE = 2
DEFAULT_MAX_SIZE = 10
DEFAULT_QUERY_TIMEOUT = 30.0
DEFAULT_RETRY_MAX = 3
DEFAULT_RETRY_DELAY = 1.0


class DatabasePool:
    """Async PostgreSQL connection pool manager.

    Example usage:
        db = DatabasePool("postgresql://hcm:pwd@localhost:5432/hcm_v2")
        await db.initialize()

        async with db.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM hcm_system.roles")

        await db.shutdown()
    """

    def __init__(
        self,
        dsn: str,
        min_size: int = DEFAULT_MIN_SIZE,
        max_size: int = DEFAULT_MAX_SIZE,
        query_timeout: float = DEFAULT_QUERY_TIMEOUT,
        retry_max: int = DEFAULT_RETRY_MAX,
        retry_delay: float = DEFAULT_RETRY_DELAY,
    ):
        """Initialize DatabasePool.

        Args:
            dsn: PostgreSQL connection string (postgresql://user:pass@host:port/db).
            min_size: Minimum pool size.
            max_size: Maximum pool size.
            query_timeout: Default query timeout in seconds.
            retry_max: Max retries for connection establishment.
            retry_delay: Initial retry delay (doubles each retry).
        """
        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._query_timeout = query_timeout
        self._retry_max = retry_max
        self._retry_delay = retry_delay
        self._pool: Optional[asyncpg.Pool] = None
        self._initialized = False

    async def initialize(self) -> None:
        """Create the connection pool with retry logic."""
        if self._initialized:
            return

        last_error: Optional[Exception] = None
        for attempt in range(1, self._retry_max + 1):
            try:
                self._pool = await asyncpg.create_pool(
                    dsn=self._dsn,
                    min_size=self._min_size,
                    max_size=self._max_size,
                    command_timeout=self._query_timeout,
                )
                # Verify connectivity
                async with self._pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")

                self._initialized = True
                logger.info(
                    "DatabasePool connected (min=%d, max=%d, dsn=%s)",
                    self._min_size, self._max_size, self._mask_dsn(),
                )
                return

            except Exception as exc:
                last_error = exc
                if attempt < self._retry_max:
                    wait = self._retry_delay * (2 ** (attempt - 1))
                    logger.warning(
                        "DatabasePool connection attempt %d/%d failed: %s. Retrying in %.1fs...",
                        attempt, self._retry_max, exc, wait,
                    )
                    await asyncio.sleep(wait)

        raise RuntimeError(
            f"DatabasePool: failed to connect after {self._retry_max} attempts. "
            f"Last error: {last_error}"
        )

    async def shutdown(self) -> None:
        """Gracefully close the connection pool."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._initialized = False
            logger.info("DatabasePool shutdown complete")

    @asynccontextmanager
    async def acquire(self):
        """Context manager for acquiring a connection from the pool.

        Yields:
            asyncpg.Connection: A database connection.
        """
        if self._pool is None:
            raise RuntimeError("DatabasePool not initialized. Call initialize() first.")

        async with self._pool.acquire() as conn:
            yield conn

    async def execute(self, query: str, *args: Any) -> str:
        """Execute a SQL statement.

        Args:
            query: SQL query with $1, $2 placeholders.
            *args: Query parameters.

        Returns:
            Command status string (e.g., "INSERT 0 1").
        """
        async with self.acquire() as conn:
            return await conn.execute(query, *args)

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        """Execute a SELECT query and return rows.

        Args:
            query: SQL query with $1, $2 placeholders.
            *args: Query parameters.

        Returns:
            List of records.
        """
        async with self.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args: Any) -> Optional[asyncpg.Record]:
        """Execute a SELECT query and return a single row.

        Args:
            query: SQL query with $1, $2 placeholders.
            *args: Query parameters.

        Returns:
            Single record or None.
        """
        async with self.acquire() as conn:
            return await conn.fetchrow(query, *args)

    async def fetchval(self, query: str, *args: Any) -> Any:
        """Execute a SELECT query and return a single value.

        Args:
            query: SQL query with $1, $2 placeholders.
            *args: Query parameters.

        Returns:
            Single scalar value or None.
        """
        async with self.acquire() as conn:
            return await conn.fetchval(query, *args)

    async def health_check(self) -> dict:
        """Check database connectivity.

        Returns:
            Dict with status and latency info.
        """
        try:
            t0 = asyncio.get_event_loop().time()
            async with self.acquire() as conn:
                v = await conn.fetchval("SELECT 1")
            latency = (asyncio.get_event_loop().time() - t0) * 1000
            return {
                "status": "healthy",
                "latency_ms": round(latency, 2),
                "pool_size": self._pool.get_size() if self._pool else 0,
            }
        except Exception as exc:
            return {"status": "unhealthy", "error": str(exc)}

    @property
    def is_initialized(self) -> bool:
        """Check if pool is initialized and ready."""
        return self._initialized and self._pool is not None

    def _mask_dsn(self) -> str:
        """Return DSN with password masked for logging."""
        try:
            # Simple masking: replace password part
            if "@" in self._dsn:
                parts = self._dsn.split("@")
                if len(parts) >= 2 and ":" in parts[0]:
                    user_host = parts[0].split(":")
                    return f"{user_host[0]}:****@{parts[1]}"
            return "***"
        except Exception:
            return "***"
