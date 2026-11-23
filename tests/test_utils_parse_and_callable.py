from typing import Any

import pytest
from pydantic import BaseModel

from tenxgraph_api.src.app.core.config.settings import Settings
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
