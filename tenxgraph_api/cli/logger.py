"""Centralized logging configuration for the 10xGraph CLI."""

from __future__ import annotations

import logging
import sys
from typing import TextIO

from .constants import LOG_DATE_FORMAT, LOG_FORMAT


_LOGGER_NAME = "tenxgraph_api.cli"


class CLILoggerMixin:
    """Mixin that obtains a child of the invocation-wide CLI logger."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.logger = get_logger(self.__class__.__name__)


def get_logger(
    name: str,
    level: int = logging.INFO,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Return a child logger governed by the shared root configuration.

    A custom stream is intended for isolated tests and receives a local handler.
    Normal command loggers propagate to exactly one root handler so verbosity
    changes apply consistently and messages cannot be duplicated.
    """
    logger = logging.getLogger(f"{_LOGGER_NAME}.{name}")

    if stream is not None:
        logger.handlers.clear()
        logger.setLevel(level)
        handler = logging.StreamHandler(stream)
        handler.setLevel(level)
        handler.setFormatter(_formatter())
        logger.addHandler(handler)
        logger.propagate = False
        return logger

    root_logger = logging.getLogger(_LOGGER_NAME)
    if not root_logger.handlers:
        setup_cli_logging(level=level)
    logger.handlers.clear()
    logger.setLevel(logging.NOTSET)
    logger.propagate = True
    return logger


def setup_cli_logging(
    level: int = logging.INFO,
    quiet: bool = False,
    verbose: bool = False,
) -> None:
    """Configure the one shared CLI logging handler."""
    if quiet:
        level = logging.ERROR
    elif verbose:
        level = logging.DEBUG

    root_logger = logging.getLogger(_LOGGER_NAME)
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(_formatter())
    root_logger.addHandler(handler)
    root_logger.propagate = False


def _formatter() -> logging.Formatter:
    return logging.Formatter(fmt=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
