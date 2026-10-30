"""G-114, G-115, G-116: log sanitizer and logging setup."""

# ruff: noqa: S101

import io
import logging
import uuid

import pytest
from pydantic import BaseModel, ValidationError

from tenxgraph_api.src.app.core.config import setup_logs
from tenxgraph_api.src.app.core.utils.log_sanitizer import (
    SanitizingFormatter,
    redact_text,
    sanitize_for_logging,
    summarize_validation_error,
)


# G-115


def test_uuid_ids_are_not_rewritten():
    thread_id = str(uuid.uuid4())
    assert sanitize_for_logging({"thread_id": thread_id}) == {"thread_id": thread_id}
    assert sanitize_for_logging(thread_id) == thread_id


def test_long_opaque_tokens_are_still_shortened():
    token = "a1b2c3d4e5" * 5
    assert sanitize_for_logging(token) != token


@pytest.mark.parametrize("key", ["monkey", "shipping", "author", "keyboard", "spinning", "turkey"])
def test_innocent_keys_are_not_redacted(key):
    assert sanitize_for_logging({key: "visible"}) == {key: "visible"}


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "api_key",
        "apiKey",
        "Authorization",
        "x-api-key",
        "access_token",
        "password",
        "client_secret",
        "refreshToken",
        "Cookie",
        "private_key",
    ],
)
def test_secret_keys_are_redacted(key):
    assert sanitize_for_logging({key: "value"}) == {key: "***REDACTED***"}


def _format(message, *args):
    formatter = SanitizingFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord("x", logging.INFO, __file__, 1, message, args, None)
    return formatter.format(record)


def test_newline_in_a_logged_value_cannot_forge_a_line():
    out = _format("user %s logged in", "bob\n2026-01-01 - x - INFO - forged line")
    assert "\n" not in out
    assert "\\n" in out


# G-114


@pytest.mark.parametrize(
    "secret",
    [
        "sk-proj-abcdefghijklmnopqrstuvwx",
        "sk-ant-api03-abcdefghijklmnopqrstuvwx",
        "AIzaSyA1234567890abcdefghijklmnopqrstu",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIAABCDEFGHIJKLMNOP",
    ],
)
def test_provider_keys_are_redacted_in_text(secret):
    assert secret not in redact_text(f"failed calling provider with key {secret} now")


def test_query_tokens_are_redacted_in_text():
    out = redact_text("GET /v1/graph/ws?token=abc.def.ghi&x=1 HTTP/1.1")
    assert "abc.def.ghi" not in out
    assert "x=1" in out


def test_access_log_hides_the_query_token():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("uvicorn.access")
    saved = (list(logger.handlers), list(logger.filters))
    try:
        setup_logs.init_logger(logging.INFO)
        for f in logger.filters:
            handler.addFilter(f)
        record = logger.makeRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:1", "GET", "/v1/graph/ws?token=SECRETVALUE", "1.1", 101),
            None,
        )
        assert logger.filter(record)
        assert "SECRETVALUE" not in record.getMessage()
    finally:
        logger.handlers, logger.filters = saved[0], saved[1]


class _Model(BaseModel):
    name: str


def test_validation_summary_has_no_input_values():
    with pytest.raises(ValidationError) as exc:
        _Model(name=12345678)  # type: ignore[arg-type]
    assert "12345678" in str(exc.value)
    summary = summarize_validation_error(exc.value)
    assert "12345678" not in summary
    assert "name" in summary


def test_ag_ui_invalid_input_message_does_not_echo_values():
    from tenxgraph_api.src.app.routers.ag_ui.converter import parse_run_input

    with pytest.raises(ValueError) as exc:
        parse_run_input({"threadId": "t", "runId": "r", "messages": "SECRET-BODY-TEXT"})
    assert "SECRET-BODY-TEXT" not in str(exc.value)


# G-116


def test_core_logger_records_reach_the_console_handler():
    stream = io.StringIO()
    core = logging.getLogger("tenxgraph")
    saved = (list(core.handlers), core.propagate, core.level)
    try:
        core.addHandler(logging.NullHandler())  # what a library does
        setup_logs.init_logger(logging.INFO)
        handlers = [h for h in core.handlers if isinstance(h.formatter, SanitizingFormatter)]
        assert handlers, "core records have no sanitizing handler"
        handlers[0].setStream(stream) if hasattr(handlers[0], "setStream") else None
        logging.getLogger("tenxgraph.storage.checkpointer").warning("checkpoint failed")
        assert "checkpoint failed" in stream.getvalue()
    finally:
        core.handlers[:], core.propagate = saved[0], saved[1]
        core.setLevel(saved[2])
