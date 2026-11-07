"""``resume`` on graph input, and the server-owned ``remote_tools`` run-config key."""

# ruff: noqa: S101

import pytest
from pydantic import ValidationError

from tenxgraph_api.src.app.core.validation.request_config import client_config
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import GraphInputSchema


def test_messages_are_required_without_resume():
    with pytest.raises(ValidationError, match="at least one message"):
        GraphInputSchema(messages=[])


def test_resume_alone_is_valid_even_when_null():
    assert GraphInputSchema(resume={"approved": True}).is_resume
    cancelled = GraphInputSchema(resume=None)
    assert cancelled.is_resume
    assert cancelled.resume is None


def test_plain_input_is_not_a_resume():
    graph_input = GraphInputSchema(
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    )
    assert not graph_input.is_resume


def test_clients_cannot_set_remote_tools():
    # Per-run client tools reach the model only when a trusted adapter (AG-UI) sets them.
    cleaned = client_config({"remote_tools": [{"name": "x"}], "thread_id": "t"})
    assert "remote_tools" not in cleaned
    assert cleaned["thread_id"] == "t"
