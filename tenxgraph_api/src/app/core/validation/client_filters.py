"""Client ``filters`` for thread listing and the store routes, checked before storage runs.

Filters are core's safe filter language (``tenxgraph.storage.sql_filter``): equality, ranges,
``like`` / ``ilike``, ``in`` and null checks on declared columns only, every value a bind
parameter. Core validates them again when it builds the query; checking here first turns a bad
filter into a 422 before any database work, with core's message, which names columns and
operators but never echoes values or SQL.

Tenant columns and ``user_id`` are isolation columns: the server sets them from the caller's
identity (``config["user"]``), so a filter naming one is refused (422), never applied.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from tenxgraph_api.src.app.core.validation.request_config import ClientInputError, structure_error


try:  # core >= 1.0.0 (safe filters); older cores have no column filters at all
    from tenxgraph.storage.sql_filter import (
        MAX_CONDITIONS,
        FilterError,
        FilterSchema,
        validate_filters,
    )
except ImportError:  # pragma: no cover - exercised only against an older core
    MAX_CONDITIONS = 10
    FilterError = None  # type: ignore[assignment,misc]
    FilterSchema = None  # type: ignore[assignment,misc]
    validate_filters = None  # type: ignore[assignment]


# Longest ``GET /v1/threads?filters=...`` value accepted. Ten conditions with long values and an
# ``in`` list fit well inside it; anything longer is refused before it is parsed.
MAX_FILTERS_PARAM_LENGTH = 8192

_NOT_AN_OBJECT = "filters must be a JSON object mapping column names to conditions"


def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def parse_filters_param(raw: str | None) -> dict[str, Any] | None:
    """Parse the ``filters`` query parameter (a JSON object) or raise ``ClientInputError``.

    ``None`` or an empty string means no filters. The message never repeats the input.
    """
    if raw is None or not raw.strip():
        return None
    if len(raw) > MAX_FILTERS_PARAM_LENGTH:
        raise ClientInputError(f"filters is longer than {MAX_FILTERS_PARAM_LENGTH} characters")
    try:
        value = json.loads(raw, parse_constant=_reject_constant)
    except (ValueError, RecursionError) as exc:
        raise ClientInputError(f"filters is not valid JSON; {_NOT_AN_OBJECT}") from exc
    if not isinstance(value, dict):
        raise ClientInputError(_NOT_AN_OBJECT)
    if (error := structure_error(value, "filters")) is not None:
        raise ClientInputError(error)
    return value or None


def _filter_schema(backend: Any) -> Any:
    """``backend.filter_schema`` when it is a core ``FilterSchema``, else None (no column
    filters: in-memory and SQLite checkpointers, QdrantStore, custom backends)."""
    if FilterSchema is None:
        return None
    schema = getattr(backend, "filter_schema", None)
    return schema if isinstance(schema, FilterSchema) else None


def _client_error(exc: Exception) -> ClientInputError:
    # FilterError messages are written to be safe for a client (see sql_filter's docstring).
    return ClientInputError(str(exc))


def check_thread_filters(checkpointer: Any, filters: dict[str, Any] | None) -> None:
    """Validate ``GET /v1/threads`` filters against the checkpointer's filter schema.

    Raises:
        ClientInputError: The filters are malformed, name a column that is not filterable
            (tenant columns and ``user_id`` included), or the checkpointer has no filterable
            columns (the in-memory and SQLite checkpointers).
    """
    if not filters:
        return
    schema = _filter_schema(checkpointer)
    if schema is None:
        raise ClientInputError(
            f"filters are not supported by {type(checkpointer).__name__}; thread filters need "
            "a checkpointer with filterable columns (PgCheckpointer)"
        )
    try:
        validate_filters(filters, schema)
    except FilterError as exc:  # type: ignore[misc]
        raise _client_error(exc) from exc


def check_store_filters(store: Any, filters: Any) -> None:
    """Validate store ``filters`` against ``store.filter_schema`` when the store has one.

    A store with a filter schema (``PgVectorStore``) takes column conditions on its
    filterable columns and plain equality on any other key, which it matches against the
    memory's JSON metadata. The column conditions are validated here with core's validator;
    the store checks the metadata keys itself (a ``FilterError`` it raises is mapped by
    :func:`filter_errors_as_client_errors`). A store without a schema (``QdrantStore``) keeps
    its own filter handling unchanged.

    Raises:
        ClientInputError: A column condition is not allowed, a tenant column or ``user_id``
            is named, or there are too many conditions.
    """
    schema = _filter_schema(store)
    if schema is None or not filters:
        return
    if not isinstance(filters, dict):
        raise ClientInputError(_NOT_AN_OBJECT)
    if len(filters) > MAX_CONDITIONS:
        raise ClientInputError(f"Too many filter conditions: at most {MAX_CONDITIONS}")
    columns = {
        key: value
        for key, value in filters.items()
        if schema.get(key) is not None or schema.is_isolation(key)
    }
    try:
        validate_filters(columns, schema)
    except FilterError as exc:  # type: ignore[misc]
        raise _client_error(exc) from exc


@contextmanager
def filter_errors_as_client_errors() -> Iterator[None]:
    """Turn a ``FilterError`` raised by storage into a ``ClientInputError`` (422, kept in
    production). Any other error passes through unchanged."""
    if FilterError is None:
        yield
        return
    try:
        yield
    except FilterError as exc:
        raise _client_error(exc) from exc
