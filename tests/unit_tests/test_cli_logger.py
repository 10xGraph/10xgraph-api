"""Tests for centralized CLI logging."""

from __future__ import annotations

import logging
from io import StringIO

from tenxgraph_api.cli.logger import (
    CLILoggerMixin,
    get_logger,
    setup_cli_logging,
)


def teardown_function() -> None:
    root = logging.getLogger("tenxgraph_api.cli")
    root.handlers.clear()


def test_mixin_uses_named_child_logger() -> None:
    class ExampleCommand(CLILoggerMixin):
        pass

    command = ExampleCommand()
    assert command.logger.name == "tenxgraph_api.cli.ExampleCommand"
    assert command.logger.propagate is True
    assert command.logger.handlers == []


def test_children_share_single_root_handler() -> None:
    setup_cli_logging(verbose=True)
    first = get_logger("first")
    second = get_logger("second")
    root = logging.getLogger("tenxgraph_api.cli")
    assert len(root.handlers) == 1
    assert first.handlers == []
    assert second.handlers == []
    assert first.getEffectiveLevel() == logging.DEBUG
    assert second.getEffectiveLevel() == logging.DEBUG


def test_quiet_and_verbose_levels() -> None:
    setup_cli_logging(verbose=True)
    assert logging.getLogger("tenxgraph_api.cli").level == logging.DEBUG
    setup_cli_logging(quiet=True, verbose=True)
    assert logging.getLogger("tenxgraph_api.cli").level == logging.ERROR


def test_reconfiguration_replaces_handler() -> None:
    setup_cli_logging()
    original = logging.getLogger("tenxgraph_api.cli").handlers[0]
    setup_cli_logging(level=logging.WARNING)
    root = logging.getLogger("tenxgraph_api.cli")
    assert len(root.handlers) == 1
    assert root.handlers[0] is not original
    assert root.handlers[0].level == logging.WARNING


def test_custom_stream_is_isolated() -> None:
    stream = StringIO()
    logger = get_logger("captured", level=logging.WARNING, stream=stream)
    logger.warning("visible")
    assert "visible" in stream.getvalue()
    assert logger.propagate is False
    assert len(logger.handlers) == 1
