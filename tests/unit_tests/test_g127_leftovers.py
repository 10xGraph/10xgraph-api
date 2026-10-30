"""G-127 leftovers: size caps on open dicts, upload parsed after auth, WS and AG-UI id rule."""

# ruff: noqa: S101, PLR2004

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tenxgraph_api.src.app.core.auth.request_config import (
    MAX_CONTAINER_ITEMS,
    MAX_DICT_KEYS,
    structure_error,
)
from tenxgraph_api.src.app.routers.checkpointer.schemas.checkpointer_schemas import (
    PutThreadSchema,
)
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import (
    GraphInputSchema,
    WsGraphInputSchema,
)
from tenxgraph_api.src.app.routers.store.schemas.store_schemas import SearchMemorySchema


MSG = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def test_structure_error_caps_keys_items_and_total():
    assert structure_error({str(i): 1 for i in range(MAX_DICT_KEYS)}, "x") is None
    assert "keys" in structure_error({str(i): 1 for i in range(MAX_DICT_KEYS + 1)}, "x")
    assert structure_error([0] * MAX_CONTAINER_ITEMS, "x") is None
    assert "items" in structure_error([0] * (MAX_CONTAINER_ITEMS + 1), "x")
    bushy = [[0] * 5000 for _ in range(50)]  # each list is fine, the total is not
    assert "in all" in structure_error(bushy, "x")


def test_graph_input_refuses_wide_dicts_and_lists():
    wide = {str(i): 1 for i in range(MAX_DICT_KEYS + 1)}
    for field in ("initial_state", "config"):
        with pytest.raises(ValidationError, match="keys"):
            GraphInputSchema(messages=MSG, **{field: wide})
    with pytest.raises(ValidationError, match="items"):
        GraphInputSchema(resume=[0] * (MAX_CONTAINER_ITEMS + 1))
    with pytest.raises(ValidationError, match="keys"):
        WsGraphInputSchema(messages=MSG, config=wide)
    GraphInputSchema(messages=MSG, config={"a": 1})


def test_store_and_checkpointer_schemas_are_bounded():
    wide = {str(i): 1 for i in range(MAX_DICT_KEYS + 1)}
    with pytest.raises(ValidationError, match="keys"):
        SearchMemorySchema(query="q", filters=wide)
    with pytest.raises(ValidationError, match="keys"):
        PutThreadSchema(thread_info=wide)


@pytest.mark.asyncio
async def test_ws_frame_with_a_bad_thread_id_gets_an_error_frame_and_skip():
    import importlib
    import json
    from unittest.mock import AsyncMock, MagicMock

    graph_router = importlib.import_module("tenxgraph_api.src.app.routers.graph.router")

    frames = MagicMock()
    frames.next = AsyncMock(
        side_effect=[
            json.dumps({"messages": MSG, "config": {"thread_id": "../etc/passwd"}}),
            json.dumps({"messages": MSG, "config": {"thread_id": "new"}}),
        ]
    )
    websocket = MagicMock()
    websocket.send_text = AsyncMock()
    skipped = await graph_router._next_ws_input(frames, websocket, 1_000_000, {})
    assert skipped is graph_router._WS_SKIP
    assert "thread_id" in websocket.send_text.await_args.args[0]
    ok = await graph_router._next_ws_input(frames, websocket, 1_000_000, {})
    assert ok.config["thread_id"] == "new"
