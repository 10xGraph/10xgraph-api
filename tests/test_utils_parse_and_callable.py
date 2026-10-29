import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from tenxgraph_api.src.app.core.config.settings import Settings
from tenxgraph_api.src.app.utils.callable_helper import call_sync_or_async
from tenxgraph_api.src.app.utils.parse_output import (
    parse_message_output,
    parse_state_output,
)


class _StateModel(BaseModel):
    a: int
    b: str
    execution_meta: dict[str, Any] | None = None


class _MessageModel(BaseModel):
    content: str
    raw: dict[str, Any] | None = None


@pytest.mark.parametrize("mode", ["development", "production"])
def test_parse_state_output(mode: str):
    settings = Settings(
        MODE=mode, ALLOWED_HOST="api.example.com", ORIGINS="https://app.example.com"
    )
    model = _StateModel(a=1, b="x", execution_meta={"duration": 123})
    out = parse_state_output(settings, model)
    # Current implementation always includes execution_meta regardless of debug
    assert out["execution_meta"] == {"duration": 123}
    assert out["a"] == 1 and out["b"] == "x"


@pytest.mark.parametrize("mode", ["development", "production"])
def test_parse_message_output(mode: str):
    settings = Settings(
        MODE=mode, ALLOWED_HOST="api.example.com", ORIGINS="https://app.example.com"
    )
    model = _MessageModel(content="hello", raw={"tokens": 5})
    out = parse_message_output(settings, model)
    # raw is never returned to clients, whatever the debug flag
    assert "raw" not in out
    assert out["content"] == "hello"


def test_call_sync_or_async_sync_function():
    def sync_fn(x: int, y: int) -> int:
        return x + y

    result = asyncio.run(call_sync_or_async(sync_fn, 2, 3))
    assert result == 5


def test_call_sync_or_async_async_function():
    async def async_fn(x: int) -> int:
        await asyncio.sleep(0)  # yield control
        return x * 2

    result = asyncio.run(call_sync_or_async(async_fn, 4))
    assert result == 8


def test_call_sync_or_async_sync_returns_awaitable():
    # Edge case: sync function returns coroutine (rare but allowed in implementation)
    async def inner() -> str:
        return "done"

    def sync_returns_coroutine():
        return inner()

    result = asyncio.run(call_sync_or_async(sync_returns_coroutine))
    assert result == "done"
