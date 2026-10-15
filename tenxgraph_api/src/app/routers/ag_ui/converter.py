"""Turn an AG-UI ``RunAgentInput`` into 10xGraph graph input.

AG-UI clients send the whole conversation on every run, while the thread's checkpoint already
holds everything the graph has seen. Only the trailing user turn (or the tool results that
answer a pending client-side tool call) is new, so that is all the graph receives. Messages the
client claims came from the assistant, system or developer are never forwarded: the server's
checkpoint is the record of what the model said and was told.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ag_ui.core import RunAgentInput
from pydantic import ValidationError
from tenxgraph.core.state import (
    AudioBlock,
    DocumentBlock,
    ImageBlock,
    Message,
    TextBlock,
    ToolResultBlock,
    VideoBlock,
)
from tenxgraph.core.state.message_block import MediaRef

from tenxgraph_api.src.app.core.auth.request_config import ClientInputError


_MEDIA_BLOCKS = {
    "image": ImageBlock,
    "audio": AudioBlock,
    "video": VideoBlock,
    "document": DocumentBlock,
}

# Roles the graph accepts from a client. Assistant turns come from the checkpoint, and client
# system/developer prompts would let a caller rewrite the agent's instructions.
_CLIENT_ROLES = frozenset({"user", "tool"})


def parse_run_input(body: Any) -> RunAgentInput:
    """Validate a request body as ``RunAgentInput``.

    Older AG-UI clients (CopilotKit up to at least 1.75) still send the legacy ``binary``
    content part, which the 1.0 SDK rejects; it is rewritten to the matching media part first.

    Raises:
        ValueError: The body is not a valid ``RunAgentInput``.
    """
    if isinstance(body, dict) and isinstance(body.get("messages"), list):
        body = {**body, "messages": [_upgrade_message(m) for m in body["messages"]]}
    try:
        return RunAgentInput.model_validate(body)
    except ValidationError as exc:
        raise ClientInputError(f"Invalid AG-UI RunAgentInput: {exc}") from exc


# Limits on the tools an AG-UI client may bring to a run. The name rule matches what model
# providers accept for function names.
MAX_CLIENT_TOOLS = 64
MAX_TOOL_DESCRIPTION_CHARS = 4096
MAX_TOOL_SCHEMA_BYTES = 16 * 1024
_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def check_client_tools(tools: list[Any]) -> None:
    """Reject client tool lists that are malformed or oversized.

    Client tools put client-written text (names, descriptions, schemas) in front of the model and
    in every request to the provider, so they are bounded before anything runs.

    Raises:
        ValueError: A tool breaks one of the limits above.
    """
    if len(tools) > MAX_CLIENT_TOOLS:
        raise ClientInputError(f"At most {MAX_CLIENT_TOOLS} client tools are allowed per run")
    seen: set[str] = set()
    for tool in tools:
        if not _TOOL_NAME.match(tool.name or ""):
            raise ClientInputError(
                f"Invalid client tool name {tool.name!r}: use 1-64 letters, digits, '_' or '-'"
            )
        if tool.name in seen:
            raise ClientInputError(f"Duplicate client tool name {tool.name!r}")
        seen.add(tool.name)
        if len(tool.description or "") > MAX_TOOL_DESCRIPTION_CHARS:
            raise ClientInputError(
                f"Client tool {tool.name!r}: description is longer than "
                f"{MAX_TOOL_DESCRIPTION_CHARS} characters"
            )
        schema_size = len(json.dumps(tool.parameters or {}, default=str).encode())
        if schema_size > MAX_TOOL_SCHEMA_BYTES:
            raise ClientInputError(
                f"Client tool {tool.name!r}: parameters schema is larger than "
                f"{MAX_TOOL_SCHEMA_BYTES} bytes"
            )


def _upgrade_message(message: Any) -> Any:
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return message
    return {**message, "content": [_upgrade_part(p) for p in message["content"]]}


def _upgrade_part(part: Any) -> Any:
    if not isinstance(part, dict) or part.get("type") != "binary":
        return part
    mime_type = part.get("mimeType") or part.get("mime_type") or ""
    kind = mime_type.split("/", 1)[0]
    part_type = kind if kind in ("image", "audio", "video") else "document"
    if part.get("data"):
        source = {"type": "data", "value": part["data"], "mimeType": mime_type}
    elif part.get("url"):
        source = {"type": "url", "value": part["url"], "mimeType": mime_type}
    else:
        source = {"type": "file", "value": part.get("id") or "", "mimeType": mime_type}
    return {"type": part_type, "source": source}


def select_new_messages(
    messages: list[Any],
    known_ids: set[str] | frozenset[str] = frozenset(),
    answered_call_ids: set[str] | frozenset[str] = frozenset(),
) -> list[Any]:
    """The client messages the graph has not seen yet.

    Args:
        messages: ``RunAgentInput.messages``, the full client-side history.
        known_ids: Message ids already in the thread's checkpoint.
        answered_call_ids: Tool call ids the checkpoint already has a result for. Clients mint
            a fresh id for each tool message, so results are matched by call id instead.
    """
    turn: list[Any] = []
    for message in reversed(messages):
        if message.role == "assistant":
            break
        turn.append(message)
    turn.reverse()
    return [
        m
        for m in turn
        if m.role in _CLIENT_ROLES
        and m.id not in known_ids
        and not (m.role == "tool" and m.tool_call_id in answered_call_ids)
    ]


def to_tenxgraph_messages(messages: list[Any]) -> list[Message]:
    """Convert AG-UI user and tool messages to 10xGraph messages, keeping their ids."""
    converted: list[Message] = []
    for message in messages:
        if message.role == "user":
            blocks = _content_blocks(message.content)
            if blocks:
                converted.append(Message(message_id=message.id, role="user", content=blocks))
        elif message.role == "tool":
            error = getattr(message, "error", None)
            output = error or _text(message.content)
            result = ToolResultBlock(
                call_id=message.tool_call_id,
                output=output,
                is_error=bool(error),
                status="failed" if error else "completed",
            )
            converted.append(Message(message_id=message.id, role="tool", content=[result]))
    return converted


def _content_blocks(content: Any) -> list[Any]:
    if isinstance(content, str):
        return [TextBlock(text=content)] if content else []
    blocks: list[Any] = []
    for part in content or []:
        if part.type == "text":
            if part.text:
                blocks.append(TextBlock(text=part.text))
        elif part.type in _MEDIA_BLOCKS:
            blocks.append(_MEDIA_BLOCKS[part.type](media=_media_ref(part.source)))
    return blocks


def _media_ref(source: Any) -> MediaRef:
    mime_type = getattr(source, "mime_type", None)
    if source.type == "data":
        return MediaRef(kind="data", data_base64=source.value, mime_type=mime_type)
    if source.type == "url":
        return MediaRef(kind="url", url=source.value, mime_type=mime_type)
    return MediaRef(kind="file_id", file_id=source.value, mime_type=mime_type)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(part.text for part in content or [] if part.type == "text")
