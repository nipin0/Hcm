"""Unified structured logging for HCM v2 services.

Produces JSON-formatted logs with the following standard fields:
    - timestamp: ISO 8601 with milliseconds
    - level: DEBUG / INFO / WARNING / ERROR
    - service: service name (e.g., "hcm-signal-tower")
    - trace_id: optional correlation ID across services
    - module: source module name
    - message: log message
    - extra: diagnostic context dict

Key rules:
    - trace_id propagated across the signal production chain
    - Sensitive info (API keys, passwords) never logged
    - All WARNING/ERROR must include extra diagnostic context
    - Signal production steps should log timing information
"""

from __future__ import annotations

import logging
import os
import sys
import time
import uuid
from typing import Any, Optional


class JsonFormatter(logging.Formatter):
    """JSON log formatter for structured logging."""

    def __init__(self, service_name: str = ""):
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        import json

        log_entry: dict[str, Any] = {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%S.", time.gmtime(record.created)
            ) + f"{int((record.created % 1) * 1000):03d}Z",
            "level": record.levelname,
            "service": self.service_name,
            "module": record.name,
            "message": record.getMessage(),
        }

        # Add trace_id if present
        trace_id = getattr(record, "trace_id", None)
        if trace_id:
            log_entry["trace_id"] = trace_id

        # Add extra context
        extra = getattr(record, "extra", None)
        if extra:
            log_entry["extra"] = extra

        # Include exception info
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = str(record.exc_info[1])

        return json.dumps(log_entry, ensure_ascii=False, default=str)


class TraceLogger(logging.LoggerAdapter):
    """Logger adapter that injects trace_id into log records."""

    def __init__(self, logger: logging.Logger, trace_id: str = ""):
        super().__init__(logger, {"trace_id": trace_id})
        self._trace_id = trace_id

    def process(self, msg: str, kwargs: dict) -> tuple[str, dict]:
        """Inject trace_id and extra into log record."""
        extra = kwargs.get("extra", {})
        extra["trace_id"] = self._trace_id
        kwargs["extra"] = extra
        return msg, kwargs

    def with_trace(self, trace_id: str = "") -> "TraceLogger":
        """Return a new TraceLogger with a different trace_id."""
        return TraceLogger(self.logger, trace_id)

    def new_trace(self) -> str:
        """Generate a new trace_id and return it."""
        self._trace_id = uuid.uuid4().hex[:12]
        return self._trace_id


def setup_logging(
    service_name: str = "",
    level: str = "INFO",
    log_file: Optional[str] = None,
) -> TraceLogger:
    """Configure structured JSON logging for a service.

    Args:
        service_name: Service identifier (e.g., "hcm-signal-tower").
        level: Log level (DEBUG/INFO/WARNING/ERROR).
        log_file: Optional file path for log output (default: stdout).

    Returns:
        TraceLogger instance for the service.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Create formatter
    formatter = JsonFormatter(service_name=service_name)

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # File handler (optional)
    if log_file:
        os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)

    # Suppress noisy third-party loggers
    for lib in ("asyncio", "asyncpg", "redis.asyncio", "uvicorn.access"):
        logging.getLogger(lib).setLevel(logging.WARNING)

    logger = logging.getLogger(service_name)
    trace_logger = TraceLogger(logger)
    trace_logger.new_trace()
    logger.info("Logging initialized for service=%s level=%s", service_name, level)

    return trace_logger


def get_logger(name: str = "") -> logging.Logger:
    """Get a standard logger for a module.

    Args:
        name: Logger name (usually __name__).

    Returns:
        logging.Logger instance.
    """
    return logging.getLogger(name)
