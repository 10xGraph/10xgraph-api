"""Client filters on extra columns of non-table types (jsonb, text[], ...) allow only is_null.

The API validates with core's ``validate_filters``; this checks the rule reaches the client as
a 422-style ``ClientInputError`` with core's message.
"""

from __future__ import annotations

import pytest
from tenxgraph.storage.columns import parse_columns
from tenxgraph.storage.sql_filter import FilterSchema

from tenxgraph_api.src.app.core.validation.client_filters import (
    check_store_filters,
    check_thread_filters,
)
from tenxgraph_api.src.app.core.validation.request_config import ClientInputError


COLUMNS = parse_columns(["org_id"], {"settings": "jsonb", "tags": "text[]", "plan": "text"})


class _Checkpointer:
    filter_schema = FilterSchema.for_threads(COLUMNS)


class _Store:
    filter_schema = FilterSchema.for_memories(COLUMNS)


@pytest.mark.parametrize(
    "filters",
    [{"settings": {"eq": "x"}}, {"tags": ["a"]}, {"tags": {"in": ["a"]}}],
)
def test_other_operators_rejected(filters):
    with pytest.raises(ClientInputError, match="supports only is_null"):
        check_thread_filters(_Checkpointer(), filters)
    with pytest.raises(ClientInputError, match="supports only is_null"):
        check_store_filters(_Store(), filters)


def test_is_null_and_known_types_pass():
    filters = {"settings": {"is_null": True}, "tags": {"is_null": False}, "plan": "pro"}
    check_thread_filters(_Checkpointer(), filters)
    check_store_filters(_Store(), filters)


def test_tenant_column_still_refused():
    with pytest.raises(ClientInputError, match="isolation column"):
        check_thread_filters(_Checkpointer(), {"org_id": {"is_null": True}})
