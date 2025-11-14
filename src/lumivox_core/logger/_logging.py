"""Implementation details for :mod:`lumivox_core.logger`."""

from __future__ import annotations

import io
import sys
import copy
import json
import queue
import atexit
import logging
import threading
from typing import Any, Mapping, MutableMapping, cast
from pathlib import Path
from dataclasses import dataclass
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler

import structlog
from rich.text import Text
from rich.syntax import Syntax
from rich.console import Console

from .logger import Logger, LogMode, LogLevel, LoggingConfig


class _BlockingQueueHandler(QueueHandler):
    """QueueHandler that favors delivery over silently dropping records."""

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # Formatting belongs to the final sink so console, file, and Loki can differ.
        return copy.copy(record)

    def enqueue(self, record: logging.LogRecord) -> None:
        cast(queue.Queue[logging.LogRecord], self.queue).put(record)


class _BlockingQueueListener(QueueListener):
    def enqueue_sentinel(self) -> None:
        cast(queue.Queue[object], self.queue).put(cast(Any, self)._sentinel)


class _RichRenderer:
    """Render a structured event for an interactive terminal."""

    _LEVEL_STYLES = {
        "debug": "dim blue",
        "info": "green",
        "warning": "yellow",
        "error": "red bold",
        "critical": "red bold reverse",
    }

    def __call__(self, _: Any, __: str, event_dict: MutableMapping[str, Any]) -> str:
        timestamp = str(event_dict.pop("timestamp", ""))
        level = str(event_dict.pop("level", "info"))
        event = str(event_dict.pop("event", ""))
        logger_name = event_dict.pop("logger", None)
        exception = event_dict.pop("exception", None)

        buffer = io.StringIO()
        # Processors run in application threads, so the Console cannot be shared.
        console = Console(file=buffer, force_terminal=True, width=120)
        prefix = Text(timestamp, style="dim cyan")
        prefix.append(" ")
        prefix.append(f"{level.upper():8}", style=self._LEVEL_STYLES.get(level, "white"))
        prefix.append(" ")
        prefix.append(event, style="bold white")
        if logger_name:
            prefix.append(f" [{logger_name}]", style="dim")
        console.print(prefix)

        for key, value in event_dict.items():
            if isinstance(value, (dict, list, tuple)):
                try:
                    rendered = json.dumps(value, indent=2, ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    rendered = str(value)
                console.print(Text(f"  {key}:", style="cyan"))
                console.print(
                    Syntax(
                        rendered,
                        "json",
                        theme="monokai",
                        line_numbers=False,
                        word_wrap=True,
                        background_color="default",
                    )
                )
            else:
                line = Text(f"  {key} = ", style="cyan")
                line.append(str(value), style="yellow")
                console.print(line)

        if exception:
            console.print(Text(str(exception), style="red"))

        return buffer.getvalue().rstrip("\n")


def _redact_value(value: Any, redacted_keys: frozenset[str]) -> Any:
    if isinstance(value, Mapping):
        return {
            key: "[REDACTED]"
            if str(key).lower().replace("-", "_") in redacted_keys
            else _redact_value(item, redacted_keys)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item, redacted_keys) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item, redacted_keys) for item in value)
    return value


def _redact_processor(redacted_keys: frozenset[str]) -> structlog.types.Processor:
    redacted_keys = frozenset(key.lower().replace("-", "_") for key in redacted_keys)

    def redact(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], _redact_value(event_dict, redacted_keys))

    return redact


def _renderer_processors(renderer: structlog.types.Processor) -> list[structlog.types.Processor]:
    return [
        structlog.processors.format_exc_info,
        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
        renderer,
    ]


@dataclass(slots=True)
class _LoggingState:
    config: LoggingConfig
    listener: _BlockingQueueListener
    queue_handler: _BlockingQueueHandler
    sink_handlers: list[logging.Handler]
    previous_root_handlers: list[logging.Handler]
    previous_root_level: int
    previous_logger_levels: dict[str, int]


_state: _LoggingState | None = None
_state_lock = threading.RLock()
_atexit_registered = False


def _normalize_level(level: LogLevel) -> int:
    if isinstance(level, int):
        return level
    normalized = logging.getLevelName(level.upper())
    if not isinstance(normalized, int):
        raise ValueError(f"Unknown log level: {level!r}")
    return normalized


def _make_formatter(
    mode: LogMode,
    redacted_keys: frozenset[str],
    *,
    terminal: bool,
) -> structlog.stdlib.ProcessorFormatter:
    if terminal and mode is LogMode.DEV:
        renderer: structlog.types.Processor = _RichRenderer()
    elif terminal and mode is LogMode.DEBUG:
        renderer = structlog.processors.JSONRenderer(indent=2, ensure_ascii=False, default=str)
    else:
        renderer = structlog.processors.JSONRenderer(ensure_ascii=False, default=str)

    foreign_pre_chain: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _redact_processor(redacted_keys),
    ]
    return structlog.stdlib.ProcessorFormatter(
        processors=_renderer_processors(renderer),
        foreign_pre_chain=foreign_pre_chain,
    )


def _make_sink_handlers(config: LoggingConfig, mode: LogMode, level: int) -> list[logging.Handler]:
    handlers: list[logging.Handler] = []
    if config.console:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(level)
        console_handler.setFormatter(_make_formatter(mode, config.redacted_keys, terminal=True))
        handlers.append(console_handler)

    if config.file:
        if config.file.max_bytes <= 0:
            raise ValueError("file.max_bytes must be positive")
        if config.file.backup_count < 0:
            raise ValueError("file.backup_count cannot be negative")
        path = Path(config.file.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path,
            maxBytes=config.file.max_bytes,
            backupCount=config.file.backup_count,
            encoding="utf-8",
        )
        file_level = level if config.file.level is None else _normalize_level(config.file.level)
        file_handler.setLevel(file_level)
        file_handler.setFormatter(_make_formatter(mode, config.redacted_keys, terminal=False))
        handlers.append(file_handler)

    if config.loki:
        try:
            from logging_loki import LokiHandler  # type: ignore[import-not-found]
        except ImportError as error:
            raise RuntimeError("Loki logging requires the 'python-logging-loki-v2' package.") from error
        tags = {"application": config.application, "environment": mode.value, **config.loki.labels}
        loki_handler = LokiHandler(url=config.loki.url, tags=tags, version="1")
        loki_level = level if config.loki.level is None else _normalize_level(config.loki.level)
        loki_handler.setLevel(loki_level)
        loki_handler.setFormatter(_make_formatter(mode, config.redacted_keys, terminal=False))
        handlers.append(cast(logging.Handler, loki_handler))

    if not handlers:
        raise ValueError("At least one logging sink must be configured.")
    return handlers


def _minimum_sink_level(config: LoggingConfig, default_level: int) -> int:
    levels = [default_level]
    if config.file and config.file.level is not None:
        levels.append(_normalize_level(config.file.level))
    if config.loki and config.loki.level is not None:
        levels.append(_normalize_level(config.loki.level))
    return min(levels)


def _configure_logging(config: LoggingConfig) -> None:
    """Configure the process-wide logging pipeline exactly once."""

    global _atexit_registered, _state
    if not config.application:
        raise ValueError("application must not be empty")
    if config.queue_size <= 0:
        raise ValueError("queue_size must be positive")
    mode = LogMode(config.mode)
    level = _normalize_level(config.level)

    with _state_lock:
        if _state is not None:
            raise RuntimeError("Logging is already configured. Call shutdown_logging() before reconfiguring.")

        structlog.reset_defaults()
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.stdlib.add_logger_name,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                structlog.processors.StackInfoRenderer(),
                _redact_processor(config.redacted_keys),
                structlog.processors.format_exc_info,
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )

        sink_handlers = _make_sink_handlers(config, mode, level)
        root_logger = logging.getLogger()
        previous_root_handlers = root_logger.handlers[:]
        previous_root_level = root_logger.level
        for handler in previous_root_handlers:
            root_logger.removeHandler(handler)
        root_logger.setLevel(_minimum_sink_level(config, level))

        log_queue: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=config.queue_size)
        queue_handler = _BlockingQueueHandler(log_queue)
        queue_handler.setLevel(logging.NOTSET)
        root_logger.addHandler(queue_handler)
        listener = _BlockingQueueListener(log_queue, *sink_handlers, respect_handler_level=True)
        listener.start()
        previous_logger_levels = {
            logger_name: logging.getLogger(logger_name).level for logger_name in config.noisy_loggers
        }
        _state = _LoggingState(
            config,
            listener,
            queue_handler,
            sink_handlers,
            previous_root_handlers,
            previous_root_level,
            previous_logger_levels,
        )

        for logger_name in config.noisy_loggers:
            logging.getLogger(logger_name).setLevel(logging.WARNING)

        if not _atexit_registered:
            atexit.register(_shutdown_logging)
            _atexit_registered = True


def _get_logger(**initial_context: Any) -> Logger:
    """Return an application logger with immutable bound context."""

    with _state_lock:
        if _state is None:
            raise RuntimeError("Logging is not configured. Call configure_logging() in the application entrypoint.")
        context = {**initial_context, "application": _state.config.application}
        return cast(Logger, structlog.get_logger(_state.config.application).bind(**context))


def _shutdown_logging() -> None:
    """Flush queued records and release configured sinks."""

    global _state
    with _state_lock:
        if _state is None:
            return
        state = _state
        _state = None

        root_logger = logging.getLogger()
        root_logger.removeHandler(state.queue_handler)
        state.listener.stop()
        state.queue_handler.close()
        for handler in state.sink_handlers:
            handler.close()
        root_logger.setLevel(state.previous_root_level)
        for handler in state.previous_root_handlers:
            root_logger.addHandler(handler)
        for logger_name, level in state.previous_logger_levels.items():
            logging.getLogger(logger_name).setLevel(level)
        structlog.reset_defaults()
