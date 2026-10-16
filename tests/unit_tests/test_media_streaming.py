"""Downloads stream from disk; uploads are read once."""

# ruff: noqa: S101

import io

import pytest
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.testclient import TestClient
from tenxgraph.storage.media.storage.local_store import LocalFileMediaStore
from tenxgraph.storage.media.storage.memory_store import InMemoryMediaStore

from tenxgraph_api.src.app.core.config.media_settings import MediaSettings, MediaStorageType
from tenxgraph_api.src.app.routers.media import MediaService
from tenxgraph_api.src.app.routers.media.router import (
    _download_response,
    _file_download_response,
    _read_upload,
)


def _service(store) -> MediaService:
    service = MediaService(settings=MediaSettings(MEDIA_STORAGE_TYPE=MediaStorageType.MEMORY))
    service._store = store  # type: ignore[attr-defined]
    return service


@pytest.fixture
def local_store(tmp_path):
    return LocalFileMediaStore(base_dir=str(tmp_path))


# ---------------------------------------------------------------------------
# downloads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_file_resolves_a_stored_file(local_store):
    key = await local_store.store(b"%PDF-1.7 ...", "application/pdf", {"owner_id": "alice"})
    service = _service(local_store)
    assert service.store is local_store
    path, mime = await service.local_file(key)
    assert path.read_bytes() == b"%PDF-1.7 ..."
    assert mime == "application/pdf"


@pytest.mark.asyncio
async def test_local_file_is_none_for_other_stores_and_bad_keys(local_store):
    memory = InMemoryMediaStore()
    key = await memory.store(b"x", "text/plain")
    assert await _service(memory).local_file(key) is None
    local_service = _service(local_store)
    assert await local_service.local_file("not-a-key") is None
    assert await local_service.local_file("0" * 32) is None  # well-formed, missing


@pytest.mark.parametrize(
    ("mime", "served", "disposition", "csp"),
    [
        ("text/html", "application/octet-stream", "attachment", True),
        ("image/png", "image/png", "inline", True),
        ("application/pdf", "application/pdf", "inline", False),
    ],
)
def test_streamed_download_matches_the_bytes_path(tmp_path, mime, served, disposition, csp):
    path = tmp_path / "f.bin"
    path.write_bytes(b"payload")
    app = FastAPI()

    @app.get("/stream")
    async def stream():
        return _file_download_response("f1", path, mime)

    @app.get("/bytes")
    async def as_bytes():
        return _download_response("f1", b"payload", mime)

    client = TestClient(app)
    streamed, buffered = client.get("/stream"), client.get("/bytes")
    assert streamed.content == buffered.content == b"payload"
    for name in ("content-type", "content-disposition", "x-content-type-options"):
        assert streamed.headers[name] == buffered.headers[name]
    assert streamed.headers["content-type"].startswith(served)
    assert streamed.headers["content-disposition"].startswith(disposition)
    assert ("content-security-policy" in streamed.headers) is csp
    assert isinstance(_file_download_response("f1", path, mime), FileResponse)


# ---------------------------------------------------------------------------
# uploads
# ---------------------------------------------------------------------------


def _upload(data: bytes, size: int | None) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename="a.txt", size=size)


@pytest.mark.asyncio
async def test_known_size_upload_is_read_once():
    reads = []
    upload = _upload(b"x" * 3000, 3000)
    real_read = upload.read

    async def counting_read(size=-1):
        reads.append(size)
        return await real_read(size)

    upload.read = counting_read  # type: ignore[method-assign]
    assert await _read_upload(upload, 4096, 1.0) == b"x" * 3000
    assert reads == [-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [5000, None])
async def test_oversized_upload_is_refused(size):
    with pytest.raises(HTTPException) as exc:
        await _read_upload(_upload(b"x" * 5000, size), 4096, 0.004)
    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_understated_size_is_still_capped():
    with pytest.raises(HTTPException) as exc:
        await _read_upload(_upload(b"x" * 5000, 10), 4096, 0.004)
    assert exc.value.status_code == 413


@pytest.mark.asyncio
async def test_unknown_size_upload_is_read_in_chunks():
    data = b"y" * (3 * 1024 * 1024 + 7)
    assert await _read_upload(_upload(data, None), len(data), 4.0) == data
