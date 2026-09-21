"""Structured logging on top of the standard library.

Console output is human-readable in dev (``format: console``) or JSON lines (``format: json``).
An optional log file always receives JSON lines. Context such as ``request_id`` or
``dataset_version`` is bound with :func:`bind_context` / :func:`bound_context` and attached to
every log record emitted in the same context (thread / asyncio task).
"""

from __future__ import annotations

import logging
import sys
from typing import IO, Any

import structlog
from structlog.contextvars import (
    bind_contextvars,
    bound_contextvars,
    clear_contextvars,
    unbind_contextvars,
)

from carquery.config import LoggingConfig

__all__ = [
    "bind_context",
    "bound_context",
    "clear_context",
    "configure_logging",
    "get_logger",
    "unbind_context",
]

bind_context = bind_contextvars
bound_context = bound_contextvars
clear_context = clear_contextvars
unbind_context = unbind_contextvars

_HANDLER_MARK = "_carquery_handler"


def configure_logging(config: LoggingConfig | None = None, stream: IO[str] | None = None) -> None:
    """Configure structlog and the root stdlib logger. Safe to call more than once."""
    config = config or LoggingConfig()
    stream = stream or sys.stderr

    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _HANDLER_MARK, False)]:
        root.removeHandler(handler)
        handler.close()

    console_renderer: Any = (
        structlog.processors.JSONRenderer()
        if config.format == "json"
        else structlog.dev.ConsoleRenderer(colors=_is_tty(stream))
    )
    root.addHandler(_handler(logging.StreamHandler(stream), shared, console_renderer, config))
    if config.file is not None:
        config.file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(config.file, encoding="utf-8")
        root.addHandler(_handler(file_handler, shared, structlog.processors.JSONRenderer(), config))
    root.setLevel(config.level)


def get_logger(name: str | None = None, **initial_context: Any) -> structlog.stdlib.BoundLogger:
    """Return a structured logger, optionally with context bound to it."""
    return structlog.stdlib.get_logger(name).bind(**initial_context)


def _handler(
    handler: logging.Handler,
    shared: list[Any],
    renderer: Any,
    config: LoggingConfig,
) -> logging.Handler:
    processors: list[Any] = [structlog.stdlib.ProcessorFormatter.remove_processors_meta]
    if isinstance(renderer, structlog.processors.JSONRenderer):
        processors.append(structlog.processors.dict_tracebacks)
    processors.append(renderer)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(foreign_pre_chain=shared, processors=processors)
    )
    handler.setLevel(config.level)
    setattr(handler, _HANDLER_MARK, True)
    return handler


def _is_tty(stream: IO[str]) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False
