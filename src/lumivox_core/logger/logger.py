"""Public structured logging API for Lumivox applications and libraries."""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any, Self, Mapping, Protocol, TypeAlias
from pathlib import Path
from dataclasses import field, dataclass

LogLevel: TypeAlias = int | str


class LogMode(StrEnum):
    DEV = "dev"
    DEBUG = "debug"
    PROD = "prod"


class Logger(Protocol):
    """The bound logger contract accepted by LogicLab libraries."""

    def bind(self, **new_values: Any) -> Self: ...

    def debug(self, event: str, **kwargs: Any) -> None: ...

    def info(self, event: str, **kwargs: Any) -> None: ...

    def warning(self, event: str, **kwargs: Any) -> None: ...

    def error(self, event: str, **kwargs: Any) -> None: ...

    def critical(self, event: str, **kwargs: Any) -> None: ...

    def exception(self, event: str, **kwargs: Any) -> None: ...


@dataclass(frozen=True, slots=True)
class RotatingFileConfig:
    path: str | Path
    max_bytes: int = 50 * 1024 * 1024
    backup_count: int = 5
    level: LogLevel | None = None


@dataclass(frozen=True, slots=True)
class LokiConfig:
    url: str
    labels: Mapping[str, str] = field(default_factory=dict)
    level: LogLevel | None = None


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """Configuration passed once by an application entrypoint."""

    application: str
    mode: LogMode = LogMode.PROD
    level: LogLevel = logging.INFO
    console: bool = True
    file: RotatingFileConfig | None = None
    loki: LokiConfig | None = None
    queue_size: int = 10_000
    noisy_loggers: tuple[str, ...] = (
        "hpack",
        "httpx",
        "httpcore",
        "openai",
        "urllib3",
        "watchfiles",
    )
    redacted_keys: frozenset[str] = frozenset(
        {"api_key", "authorization", "cookie", "password", "secret", "set_cookie", "token", "x_api_key"}
    )


def configure_logging(config: LoggingConfig) -> None:
    """Configure the process-wide logging pipeline exactly once.

    Call this from an application entrypoint. Libraries should receive a
    :class:`Logger` from their caller instead of configuring logging themselves.
    """

    from ._logging import _configure_logging

    _configure_logging(config)


def get_logger(**initial_context: Any) -> Logger:
    """Return an application logger with immutable bound context."""

    from ._logging import _get_logger

    return _get_logger(**initial_context)


def shutdown_logging() -> None:
    """Flush queued records and release configured sinks."""

    from ._logging import _shutdown_logging

    _shutdown_logging()


__all__ = [
    "Logger",
    "LogLevel",
    "LogMode",
    "LoggingConfig",
    "LokiConfig",
    "RotatingFileConfig",
    "configure_logging",
    "get_logger",
    "shutdown_logging",
]
