import json
import logging
import sys
from datetime import UTC, datetime

from fastapi.logger import logger as fastapi_logger

from tenxgraph_api.src.app.core.config.settings import LOGGER_NAME
from tenxgraph_api.src.app.core.request_context import get_request_context
from tenxgraph_api.src.app.core.utils.log_sanitizer import SanitizingFormatter, redact_text


# Top-level names of the loggers the API server writes to; children ("tenxgraph_api.media",
# "tenxgraph_api.rate_limit", ...) reach these handlers through propagation.
# "tenxgraph" is the core framework: its records (checkpoint failures, provider retries,
# publisher errors) used to be dropped because the library only installs a NullHandler.
APP_LOGGER_NAMES = (LOGGER_NAME, "10xgraph-api", "tenxgraph_api", "tenxgraph")


class AccessLogTokenFilter(logging.Filter):
    """Redact credentials in the request line of uvicorn's access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                redact_text(arg) if isinstance(arg, str) else arg for arg in record.args
            )
        return True


class RequestContextFilter(logging.Filter):
    """Copy the current request context (request id, transport, user) onto the record."""

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = get_request_context()
        record.request_id = ctx.request_id if ctx else None
        record.transport = ctx.transport if ctx else None
        record.user_id = ctx.user_id if ctx else None
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per record. Wrapped by ``SanitizingFormatter``, so secrets are masked."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")[
                :-6
            ]
            + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "transport", "user_id"):
            if getattr(record, key, None):
                entry[key] = getattr(record, key)
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def init_logger(level: int | str = logging.INFO, log_format: str = "text") -> None:
    """
    Initializes and configures logging for the application.

    This function sets up various loggers used in the application, including
    those for Gunicorn, Uvicorn, FastAPI, database clients, Tortoise ORM, and
    custom loggers. It also configures a console handler to output logs to
    stdout.

    Args:
        level (int): The logging level to set for the loggers.
    """
    # GCLOUD SETUP
    # client = Client()
    # client.get_default_handler()
    # client.setup_logging()

    # setup logging
    gunicorn_error_logger = logging.getLogger("gunicorn.error")
    # gunicorn_logger = logging.getLogger("gunicorn")
    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.handlers = gunicorn_error_logger.handlers
    # The request line is logged as written, query string included (``?token=...``).
    if not any(isinstance(f, AccessLogTokenFilter) for f in uvicorn_access_logger.filters):
        uvicorn_access_logger.addFilter(AccessLogTokenFilter())
    fastapi_logger.handlers = gunicorn_error_logger.handlers
    fastapi_logger.setLevel(level)

    # Create console handler and set level to DEBUG
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.addFilter(RequestContextFilter())

    # Create formatter
    base_formatter: logging.Formatter = (
        JsonFormatter()
        if log_format == "json"
        else logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s"
        )
    )
    # Wrap with sanitizing formatter to prevent sensitive data in logs
    formatter = SanitizingFormatter(base_formatter)

    # Add formatter to console handler
    console_handler.setFormatter(formatter)
    # Add console handler to logger
    fastapi_logger.addHandler(console_handler)

    # Route application loggers through the same sanitizing console handler. They stop
    # propagating so an unsanitized copy does not also reach the root (or last-resort) handler.
    for logger_name in dict.fromkeys(APP_LOGGER_NAMES):
        app_logger = logging.getLogger(logger_name)
        app_logger.setLevel(level)
        for handler in list(app_logger.handlers):
            if isinstance(handler.formatter, SanitizingFormatter):
                app_logger.removeHandler(handler)  # init_logger may run more than once
        app_logger.addHandler(console_handler)
        app_logger.propagate = False
