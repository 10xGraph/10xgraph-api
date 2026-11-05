"""Every internal media reference in client input must pass the file ownership check (H9).

Uploads are referenced as ``graph://media/<key>`` (legacy: ``agentflow://media/<key>``) once
inside the graph. The ownership check used to run only for ``kind: "file_id"`` blocks, so a
client could send the internal URL form directly and have the graph read another user's file.
"""

# ruff: noqa: S101

from unittest.mock import AsyncMock, MagicMock

import pytest
from tenxgraph.core.state import Message
from tenxgraph.core.state.message_block import (
    DocumentBlock,
    ImageBlock,
    MediaRef,
    TextBlock,
    ToolResultBlock,
)

from tenxgraph_api.src.app.routers.graph.services.multimodal_preprocessor import (
    preprocess_multimodal_messages,
)


def _service(owners: dict[str, str]) -> MagicMock:
    """A media service whose ownership check knows the given ``file -> owner`` map."""

    async def ensure_can_access(file_id, user_id, user=None):
        if file_id not in owners:
            raise KeyError(file_id)
        if owners[file_id] != user_id:
            raise PermissionError(file_id)

    service = MagicMock()
    service.ensure_can_access = AsyncMock(side_effect=ensure_can_access)
    service.get_cached_extraction.return_value = None
    return service


def _image(url: str) -> Message:
    return Message(role="user", content=[ImageBlock(media=MediaRef(kind="url", url=url))])


@pytest.mark.asyncio
async def test_internal_url_of_another_users_file_is_rejected():
    service = _service({"victim-file": "victim"})
    with pytest.raises(ValueError, match="not found"):
        await preprocess_multimodal_messages(
            [_image("graph://media/victim-file")], service, "attacker"
        )
    service.ensure_can_access.assert_awaited_once_with("victim-file", "attacker", None)


@pytest.mark.asyncio
async def test_legacy_internal_url_of_another_users_file_is_rejected():
    service = _service({"victim-file": "victim"})
    with pytest.raises(ValueError, match="not found"):
        await preprocess_multimodal_messages(
            [_image("agentflow://media/victim-file")], service, "attacker"
        )
    service.ensure_can_access.assert_awaited_once_with("victim-file", "attacker", None)


@pytest.mark.asyncio
async def test_internal_url_of_own_file_is_allowed():
    service = _service({"mine": "alice"})
    result = await preprocess_multimodal_messages([_image("graph://media/mine")], service, "alice")
    assert result[0].content[0].media.url == "graph://media/mine"


@pytest.mark.asyncio
async def test_internal_url_nested_in_a_tool_result_is_checked():
    service = _service({"victim-file": "victim"})
    nested = ToolResultBlock(
        call_id="1",
        output=[{"type": "image", "media": {"kind": "url", "url": "graph://media/victim-file"}}],
    )
    with pytest.raises(ValueError, match="not found"):
        await preprocess_multimodal_messages(
            [Message(role="tool", content=[nested])], service, "attacker"
        )


@pytest.mark.asyncio
async def test_file_id_of_another_user_gives_the_same_not_found_error():
    service = _service({"victim-file": "victim"})
    doc = DocumentBlock(media=MediaRef(kind="file_id", file_id="victim-file"))
    with pytest.raises(ValueError, match="not found"):
        await preprocess_multimodal_messages(
            [Message(role="user", content=[doc])], service, "attacker"
        )


@pytest.mark.asyncio
async def test_unknown_file_gives_the_same_not_found_error():
    service = _service({})
    with pytest.raises(ValueError, match="not found"):
        await preprocess_multimodal_messages([_image("graph://media/nope")], service, "attacker")


@pytest.mark.asyncio
async def test_internal_url_rejected_when_the_api_cannot_check_ownership():
    # No media service in the API, but the graph may still have a media store.
    with pytest.raises(ValueError, match="cannot be verified"):
        await preprocess_multimodal_messages([_image("graph://media/x")], None, "attacker")


@pytest.mark.asyncio
async def test_external_urls_and_text_are_not_checked():
    service = _service({})
    msgs = [
        _image("https://example.com/cat.png"),
        Message(role="user", content=[TextBlock(text="see graph://media/abc")]),
    ]
    result = await preprocess_multimodal_messages(msgs, service, "alice")
    assert len(result) == 2
    service.ensure_can_access.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_checks_without_an_authenticated_user():
    # Auth disabled: there is no identity to check ownership against.
    service = _service({"victim-file": "victim"})
    await preprocess_multimodal_messages([_image("graph://media/victim-file")], service, None)
    service.ensure_can_access.assert_not_awaited()


# ---------------------------------------------------------------------------
# Messages written straight into a thread are resolved on the next run too.
# ---------------------------------------------------------------------------


@pytest.fixture
def checkpointer_service(monkeypatch):
    from tenxgraph.core.state import AgentState

    from tenxgraph_api.src.app.routers.checkpointer.services.checkpointer_service import (
        CheckpointerService,
    )

    service = CheckpointerService.__new__(CheckpointerService)
    service.checkpointer = MagicMock()
    service.checkpointer.aget_state = AsyncMock(return_value=AgentState())
    service.checkpointer.aput_state = AsyncMock(side_effect=lambda cfg, state: state)
    service.checkpointer.aput_state_cache = AsyncMock()
    service.checkpointer.aput_messages = AsyncMock()
    service.settings = MagicMock()
    media = _service({"victim-file": "victim"})
    monkeypatch.setattr(CheckpointerService, "_media_service", staticmethod(lambda: media))
    return service


@pytest.mark.asyncio
async def test_put_messages_rejects_another_users_file(checkpointer_service):
    with pytest.raises(ValueError, match="not found"):
        await checkpointer_service.put_messages(
            {"thread_id": "t1"}, {"user_id": "attacker"}, [_image("graph://media/victim-file")]
        )
    checkpointer_service.checkpointer.aput_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_state_rejects_another_users_file(checkpointer_service):
    context = [
        {
            "role": "user",
            "content": [
                {"type": "image", "media": {"kind": "url", "url": "graph://media/victim-file"}}
            ],
        }
    ]
    with pytest.raises(ValueError, match="not found"):
        await checkpointer_service.put_state(
            {"thread_id": "t1"}, {"user_id": "attacker"}, {"context": context}
        )
    checkpointer_service.checkpointer.aput_state.assert_not_awaited()
