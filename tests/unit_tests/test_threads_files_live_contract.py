"""X8 (has_more on thread lists), X3 (GET /v1/files listing), X10 (realtime_options)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from tenxgraph.core.state import Message
from tenxgraph.storage.checkpointer import BaseCheckpointer
from tenxgraph.storage.media.storage.local_store import LocalFileMediaStore
from tenxgraph.storage.media.storage.memory_store import InMemoryMediaStore

from tenxgraph_api.src.app.core.config.graph_config import WebSocketConfig
from tenxgraph_api.src.app.routers.checkpointer.services.checkpointer_service import (
    CheckpointerService,
)
from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService
from tenxgraph_api.src.app.routers.media import MediaService


def _checkpointer_service(listed):
    cp = MagicMock(spec=BaseCheckpointer)

    async def alist(cfg, search, offset, limit):
        return listed[(offset or 0) : (offset or 0) + limit]

    cp.alist_messages = AsyncMock(side_effect=alist)
    cp.alist_threads = AsyncMock(side_effect=alist)
    svc = CheckpointerService.__new__(CheckpointerService)
    svc.checkpointer = cp
    svc.settings = MagicMock()
    return svc


class _T:
    def __init__(self, i):
        self.i = i

    def model_dump(self):
        return {"thread_id": str(self.i)}


class TestHasMore:
    @pytest.mark.asyncio
    async def test_threads_full_page_with_more(self):
        svc = _checkpointer_service([_T(i) for i in range(5)])
        res = await svc.list_threads({"user_id": "u"}, None, 0, 3)
        assert len(res.threads) == 3
        assert (res.has_more, res.offset, res.limit) == (True, 0, 3)

    @pytest.mark.asyncio
    async def test_threads_exact_last_page(self):
        svc = _checkpointer_service([_T(i) for i in range(3)])
        res = await svc.list_threads({"user_id": "u"}, None, 0, 3)
        assert res.has_more is False

    @pytest.mark.asyncio
    async def test_threads_short_page(self):
        svc = _checkpointer_service([_T(i) for i in range(2)])
        res = await svc.list_threads({"user_id": "u"}, None, 0, 3)
        assert res.has_more is False

    @pytest.mark.asyncio
    async def test_messages_second_page(self):
        svc = _checkpointer_service([Message.text_message(str(i)) for i in range(7)])
        res = await svc.get_messages({"thread_id": "t"}, {"user_id": "u"}, None, 3, 3)
        assert [m.text() for m in res.messages] == ["3", "4", "5"]
        assert (res.has_more, res.offset) == (True, 3)
        res = await svc.get_messages({"thread_id": "t"}, {"user_id": "u"}, None, 6, 3)
        assert res.has_more is False


class _ListableMemoryStore(InMemoryMediaStore):
    """Memory store with ``list_keys``, so the tests also pass against an older core."""

    async def list_keys(self):
        return list(self._data)


needs_core_list_keys = pytest.mark.skipif(
    not hasattr(InMemoryMediaStore, "list_keys"), reason="core without BaseMediaStore.list_keys"
)


def _media(store):
    svc = MediaService.__new__(MediaService)
    svc._settings = SimpleNamespace(MEDIA_REQUIRE_OWNER=False)
    svc._store = store
    return svc


class TestListFiles:
    @pytest.mark.asyncio
    async def test_only_own_files_paged_newest_first(self):
        store = _ListableMemoryStore()
        for i, owner in enumerate(["alice", "bob", "alice", "alice", None]):
            meta = {"filename": f"f{i}", "created_at": 100 + i}
            if owner:
                meta["owner_id"] = owner
            await store.store(b"x", "text/plain", meta)
        svc = _media(store)

        page, more = await svc.list_files("alice", 0, 2)
        assert [f["filename"] for f in page] == ["f3", "f2"]
        assert more is True
        assert page[0]["url"] == f"/v1/files/{page[0]['file_id']}"
        page, more = await svc.list_files("alice", 2, 2)
        assert [f["filename"] for f in page] == ["f0"]
        assert more is False
        page, _ = await svc.list_files("carol", 0, 10)
        assert page == []

    @pytest.mark.asyncio
    async def test_no_user_lists_everything(self):
        store = _ListableMemoryStore()
        await store.store(b"x", "text/plain", {"owner_id": "alice"})
        await store.store(b"x", "text/plain", {})
        page, _ = await _media(store).list_files(None, 0, 10)
        assert len(page) == 2

    @pytest.mark.asyncio
    async def test_store_without_listing_raises(self):
        store = SimpleNamespace(get_metadata=AsyncMock())  # no list_keys
        with pytest.raises(NotImplementedError):
            await _media(store).list_files("alice", 0, 10)

    @needs_core_list_keys
    @pytest.mark.asyncio
    async def test_local_store_lists_keys(self, tmp_path):
        store = LocalFileMediaStore(str(tmp_path))
        k = await store.store(b"hi", "text/plain", {"owner_id": "alice", "filename": "a.txt"})
        assert await store.list_keys() == [k]
        page, _ = await _media(store).list_files("alice", 0, 10)
        assert page[0]["file_id"] == k and page[0]["size_bytes"] == 2


class TestRealtimeOptions:
    def _service(self, ws, live):
        srv = GraphService.__new__(GraphService)
        srv._graph = MagicMock()
        srv._graph.generate_graph.return_value = {
            "info": {
                "node_count": 1,
                "edge_count": 0,
                "checkpointer": False,
                "publisher": False,
                "store": False,
            },
            "nodes": [],
            "edges": [],
        }
        srv.config = SimpleNamespace(websocket=ws)
        return srv

    @pytest.mark.asyncio
    async def test_live_graph_reports_options(self):
        ws = WebSocketConfig.from_dict(
            {"realtime_models": ["m1"], "realtime_client_overrides": ["voice", "system_prompt"]}
        )
        srv = self._service(ws, True)
        with patch.object(GraphService, "is_live_agent", new_callable=PropertyMock) as live:
            live.return_value = True
            res = await srv.graph_details()
        assert res.info.realtime_options.model_dump() == {
            "client_overrides": ["voice", "system_prompt"],
            "models": ["m1"],
            "model_selectable": True,
            "tools_tags": True,
        }

    @pytest.mark.asyncio
    async def test_default_and_non_live(self):
        ws = WebSocketConfig.from_dict({})
        srv = self._service(ws, True)
        with patch.object(GraphService, "is_live_agent", new_callable=PropertyMock) as live:
            live.return_value = True
            opts = (await srv.graph_details()).info.realtime_options
            assert opts.client_overrides == ["voice", "modalities", "vad"]
            assert opts.model_selectable is False
            live.return_value = False
            assert (await srv.graph_details()).info.realtime_options is None
