"""Tests for centralized CLI logging."""

from __future__ import annotations

import logging
from io import StringIO

from tenxgraph_api.cli.logger import (
    get_logger,
    setup_cli_logging,
)


def teardown_function() -> None:
    root = logging.getLogger("tenxgraph_api.cli")
    root.handlers.clear()






def test_quiet_and_verbose_levels() -> None:
    setup_cli_logging(verbose=True)
    assert logging.getLogger("tenxgraph_api.cli").level == logging.DEBUG
    setup_cli_logging(quiet=True, verbose=True)
    assert logging.getLogger("tenxgraph_api.cli").level == logging.ERROR




def test_custom_stream_is_isolated() -> None:
    stream = StringIO()
    logger = get_logger("captured", level=logging.WARNING, stream=stream)
    logger.warning("visible")
    assert "visible" in stream.getvalue()
    assert logger.propagate is False
    assert len(logger.handlers) == 1
