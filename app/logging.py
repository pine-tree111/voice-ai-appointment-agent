"""Structured JSON logging configuration with log-injection protection."""

import logging
import re
import sys
from typing import Any, cast

import structlog
from structlog.stdlib import BoundLogger
from structlog.types import EventDict, Processor, WrappedLogger

from app.config import get_settings

# SECURITY-WAIVER: regex on bounded untrusted input
_ANSI_ESCAPE_RE: re.Pattern[str] = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def sanitize_log_string(raw: str, max_length: int = 10_000) -> str:
    """Sanitize strings against log injection attacks.

    Strips null bytes, carriage returns, raw newlines, and ANSI escapes.
    """
    bounded = raw[:max_length]
    normalized = (
        bounded.replace("\0", "")
        .replace("\r", " ")
        .replace("\n", " ")
    )
    return _ANSI_ESCAPE_RE.sub("", normalized)


def _deep_sanitize(item: Any) -> Any:
    """Recursively sanitize strings inside nested structures."""
    if isinstance(item, str):
        return sanitize_log_string(item)
    if isinstance(item, dict):
        return {k: _deep_sanitize(v) for k, v in item.items()}
    if isinstance(item, list):
        return [_deep_sanitize(elem) for elem in item]
    return item


def sanitize_log_injection(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Structlog processor stripping injection payloads from log values."""
    for key, value in list(event_dict.items()):
        event_dict[key] = _deep_sanitize(value)
    return event_dict


def configure_logging() -> None:
    """Configure structlog for structured JSON logging with security filters."""
    settings = get_settings()
    level_num = getattr(logging, settings.log_level.upper(), logging.INFO)

    processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        sanitize_log_injection,
        structlog.processors.JSONRenderer(),
    ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(level_num),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "voice_agent") -> BoundLogger:
    """Obtain a contextual bound logger instance."""
    return cast(BoundLogger, structlog.get_logger(name))
