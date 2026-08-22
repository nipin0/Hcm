"""HCM v2 Shared Library.

This package provides shared infrastructure for all HCM v2 services:
- ConfigProviderV3: three-layer config cache (Local → Redis → PostgreSQL)
- Async PostgreSQL connection pool
- Redis client (Stream + PUB/SUB + Hash)
- Shared data models (Pydantic)
- Unified error codes
- Structured logging
- Prometheus metrics
"""

__version__ = "2.0.0"
