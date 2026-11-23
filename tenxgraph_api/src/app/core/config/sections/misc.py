"""Remote tools, generator names, ``ag_ui`` and ``media`` sections of 10xgraph.json."""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError
from tenxgraph.core.graph.remote_tool import RemoteToolConfig


if TYPE_CHECKING:
    pass
from tenxgraph_api.src.app.core.config.sections.common import _parse_bool


logger = logging.getLogger("tenxgraph_api")


_REMOTE_TOOLS_ADAPTER = TypeAdapter(list[RemoteToolConfig])


def validate_remote_tools(raw: object) -> list[RemoteToolConfig]:
    """Validate remote-tool declarations for both CLI audit and API startup."""
    if not isinstance(raw, list):
        raise ValueError("remote_tools must be a list")

    try:
        tools = _REMOTE_TOOLS_ADAPTER.validate_python(raw)
    except ValidationError as exc:
        raise ValueError(f"Invalid remote_tools configuration: {exc}") from exc

    seen: set[str] = set()
    for tool in tools:
        if tool.name in seen:
            raise ValueError(f"Duplicate remote tool name '{tool.name}'")
        seen.add(tool.name)
    return tools


# ``id_generator`` names: the concrete generators in core ``tenxgraph.utils.id_generator`` plus
# the API's ``snowflake`` (``SnowFlakeIdGenerator``). Mapped to classes by the app loader.
ID_GENERATOR_NAMES: tuple[str, ...] = (
    "uuid",
    "hex",
    "short",
    "int",
    "bigint",
    "timestamp",
    "snowflake",
)

# Built-in ``thread_name_generator`` choices; anything else must be ``"module:attr"``.
THREAD_NAME_CHOICES: tuple[str, ...] = ("none", "random", "first_message")


@dataclass
class AgUiConfig:
    """AG-UI protocol endpoint settings parsed from 10xgraph.json.

    Example::

        "ag_ui": {
            "enabled": true,
            "allow_client_tools": true
        }

    When enabled, ``POST /v1/ag-ui`` streams the graph as AG-UI events, so AG-UI clients
    such as CopilotKit can use the agent. It needs the ``ag-ui`` extra
    (``pip install "10xgraph-api[ag-ui]"``). Off unless explicitly enabled.

    ``allow_client_tools`` (default ``true``) offers the tools an AG-UI client sends with each
    run to the model for that run. They run in the caller's own browser, cannot take the name of
    a server tool, and must pass size limits. Set it to ``false`` to accept only the client tools
    declared under ``remote_tools``.
    """

    enabled: bool = False
    allow_client_tools: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "AgUiConfig":
        if not isinstance(data, dict):
            raise ValueError("ag_ui must be an object")
        return cls(
            enabled=_parse_bool(data.get("enabled", False), field="ag_ui.enabled"),
            allow_client_tools=_parse_bool(
                data.get("allow_client_tools", True), field="ag_ui.allow_client_tools"
            ),
        )


@dataclass
class MediaConfig:
    """Uploaded-file lifecycle settings parsed from 10xgraph.json.

    Example::

        "media": {"delete_with_thread": true}

    The API records which threads use an uploaded file (``graph://media/`` references in
    accepted messages). With ``delete_with_thread`` (default ``true``), deleting a message or a
    thread drops that link, and a file with no thread left is deleted too. Set it to ``false``
    to keep the links but never delete automatically. Files uploaded before link tracking
    existed are never deleted automatically either way.
    """

    delete_with_thread: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "MediaConfig":
        if not isinstance(data, dict):
            raise ValueError("media must be an object")
        return cls(
            delete_with_thread=_parse_bool(
                data.get("delete_with_thread", True), field="media.delete_with_thread"
            ),
        )
