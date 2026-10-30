"""G-127: a multipart upload is refused (401) before its body is parsed."""

# ruff: noqa: S101

from starlette.requests import Request

from tenxgraph_api.src.app.routers.media import MediaService
from tenxgraph_api.src.app.routers.media.router import router as media_router

from .conftest import build_app, make_client


def _app(settings):
    return build_app(
        routers=[media_router], extra_bindings={MediaService: MediaService(settings=settings)}
    )


def test_unauthenticated_upload_is_not_parsed(monkeypatch, memory_media_settings):
    parsed = []
    original = Request.form

    async def spy(self, *args, **kwargs):
        parsed.append(True)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(Request, "form", spy)
    client = make_client(_app(memory_media_settings))
    resp = client.post("/v1/files/upload", files={"file": ("a.txt", b"hello")})
    assert resp.status_code == 401
    assert parsed == []


def test_missing_file_field_is_422(memory_media_settings):
    client = make_client(_app(memory_media_settings))
    resp = client.post(
        "/v1/files/upload",
        files={"other": ("a.txt", b"hello")},
        headers={"X-Test-User": "alice"},
    )
    assert resp.status_code == 422
