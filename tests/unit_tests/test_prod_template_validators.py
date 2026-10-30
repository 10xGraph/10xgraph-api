"""The production template validator does not block ordinary text (G-56)."""

import importlib.util
from pathlib import Path

import pytest
from tenxgraph.core.state import Message


TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "tenxgraph_api/cli/templates/prod/graph/validators/validators.py"
)


def _validator():
    spec = importlib.util.spec_from_file_location("prod_validators_template", TEMPLATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.prompt_validator


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "Is the free plan limited? I need a token for the API, and how do I disable alerts?",
        "How do I override a default setting or bypass the proxy?",
        "word " * 400,
    ],
)
async def test_ordinary_text_passes(text):
    assert await _validator().validate([Message.text_message(text, role="user")]) is True
