"""Pre-processing utilities for multimodal messages at the API boundary."""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Any

from tenxgraph.core.state import Message
from tenxgraph.core.state.message_block import DocumentBlock, TextBlock
from tenxgraph.utils.media_scheme import MEDIA_SCHEME, is_internal_media_url, strip_media_scheme

from tenxgraph_api.src.app.core.auth.request_config import ClientInputError


if TYPE_CHECKING:
    from tenxgraph_api.src.app.routers.media import MediaService


logger = logging.getLogger("tenxgraph_api.media")


def _internal_media_keys(message: Message | dict[str, Any]) -> list[str]:
    """Every internal media key referenced anywhere in a message's content.

    Both ``graph://media/`` and the legacy ``agentflow://media/`` resolve to a stored
    file, so both are collected for the ownership check.

    Walks the whole content tree, not just top-level blocks, so references nested in tool
    results or other structured output are found too.
    """
    keys: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "url" and isinstance(value, str) and is_internal_media_url(value):
                    keys.append(strip_media_scheme(value))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    if isinstance(message, dict):
        walk(message.get("content"))
    else:
        walk(message.model_dump(mode="python").get("content"))
    return keys


async def _ensure_access(media_service: MediaService, file_id: str, user_id: str) -> None:
    """Ownership check with one answer for missing and foreign files.

    Both become the same ``ValueError`` (a 422 on invoke), so the response never tells a
    caller whether another user's file exists.
    """
    try:
        await media_service.ensure_can_access(file_id, user_id)
    except (KeyError, PermissionError) as exc:
        raise ClientInputError(f"File not found: {file_id}") from exc


async def check_media_references(
    messages: list[Message] | list[dict[str, Any]],
    media_service: MediaService | None,
    user_id: str,
) -> None:
    """Run the ownership check on every internal media reference a client sent.

    A client may reference ``graph://media/<key>`` directly instead of a ``file_id``;
    the graph resolves both the same way, so both must be checked. Also used for messages
    written straight into a thread, since the next run resolves those references too.
    """
    for message in messages:
        keys = list(dict.fromkeys(_internal_media_keys(message)))
        if not keys:
            continue
        if media_service is None:
            raise ClientInputError(
                "Internal media references cannot be verified: file uploads are not "
                "configured on this server."
            )
        for key in keys:
            await _ensure_access(media_service, key, user_id)


async def _get_cached_extraction(media_service: MediaService, file_id: str) -> str | None:
    """Read cached extraction from async or sync service APIs."""
    async_getter = getattr(media_service, "aget_cached_extraction", None)
    if callable(async_getter):
        result = async_getter(file_id)
        if inspect.isawaitable(result):
            return await result

    sync_getter = getattr(media_service, "get_cached_extraction", None)
    if callable(sync_getter):
        return sync_getter(file_id)

    if callable(async_getter):
        return result

    return None


async def preprocess_multimodal_messages(
    messages: list[Message],
    media_service: MediaService | None,
    user_id: str | None = None,
) -> list[Message]:
    """Resolve file_id references in messages before graph execution.

    For each ``DocumentBlock`` with a ``file_id``:
      - If cached extracted text exists, replace with a ``TextBlock``.
      - Otherwise pass through unchanged (the agent converter will handle it).

    For each ``ImageBlock``/``AudioBlock`` with a ``file_id``:
      - Convert to a ``graph://media/{file_id}`` URL-based reference so
        the MediaRefResolver can pick it up at LLM-call time.

    With an authenticated ``user_id``, every file the messages reference -- by ``file_id``
    or by an internal ``graph://media/`` URL -- must belong to that user.
    ``media_service`` being None skips the rewriting, but not the ownership rule.
    """
    if user_id:
        await check_media_references(messages, media_service, user_id)
    if media_service is None:
        return messages

    processed: list[Message] = []
    for msg in messages:
        new_content = []
        changed = False

        for block in msg.content:
            if (
                isinstance(block, DocumentBlock)
                and block.media.kind == "file_id"
                and block.media.file_id
            ):
                if user_id:
                    await _ensure_access(media_service, block.media.file_id, user_id)
                cached = await _get_cached_extraction(media_service, block.media.file_id)
                if cached:
                    new_content.append(TextBlock(text=cached))
                    changed = True
                    continue

            if hasattr(block, "media") and block.media.kind == "file_id" and block.media.file_id:
                fid = block.media.file_id
                if user_id:
                    await _ensure_access(media_service, fid, user_id)
                # Convert file_id → graph://media/ URL reference
                if not is_internal_media_url(block.media.url):
                    block.media.kind = "url"
                    block.media.url = f"{MEDIA_SCHEME}{fid}"
                    changed = True

            new_content.append(block)

        if changed:
            new_msg = msg.model_copy(update={"content": new_content})
            processed.append(new_msg)
        else:
            processed.append(msg)

    return processed
