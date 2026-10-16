"""Media surface hardening: M5, M6, L4, L6, L8."""

# ruff: noqa: S101, PLR2004

import pytest
from pydantic import ValidationError

from tenxgraph_api.src.app.core.config.media_settings import MediaSettings
from tenxgraph_api.src.app.routers.media import _BoundedCache
from tenxgraph_api.src.app.routers.media.router import _download_response
from tenxgraph_api.src.app.routers.media.schemas import MultimodalConfigResponse


# ---------------------------------------------------------------------------
# M5: MEDIA_REQUIRE_OWNER is a real setting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("false", False), ("1", True)])
def test_require_owner_is_read_from_the_environment(monkeypatch, raw, expected):
    monkeypatch.setenv("MEDIA_REQUIRE_OWNER", raw)
    assert MediaSettings(_env_file=None).MEDIA_REQUIRE_OWNER is expected


@pytest.mark.parametrize(("mode", "expected"), [("production", True), ("development", False)])
def test_require_owner_defaults_by_mode(monkeypatch, mode, expected):
    """Ownerless files are denied by default in production."""
    from tenxgraph_api.src.app.core.config import settings as settings_mod

    monkeypatch.delenv("MEDIA_REQUIRE_OWNER", raising=False)
    monkeypatch.setattr(settings_mod, "get_settings", lambda: type("S", (), {"MODE": mode})())
    assert MediaSettings(_env_file=None).MEDIA_REQUIRE_OWNER is expected


@pytest.mark.parametrize(("raw", "expected"), [("false", False), ("true", True)])
def test_explicit_require_owner_beats_the_mode_default(monkeypatch, raw, expected):
    from tenxgraph_api.src.app.core.config import settings as settings_mod

    monkeypatch.setenv("MEDIA_REQUIRE_OWNER", raw)
    for mode in ("production", "development"):
        monkeypatch.setattr(
            settings_mod, "get_settings", lambda m=mode: type("S", (), {"MODE": m})()
        )
        assert MediaSettings(_env_file=None).MEDIA_REQUIRE_OWNER is expected


# ---------------------------------------------------------------------------
# M6: uploads cannot run script on the API origin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mime",
    ["text/html", "image/svg+xml", "application/xhtml+xml", "text/xml", "application/javascript"],
)
def test_active_types_are_served_as_opaque_downloads(mime):
    response = _download_response("f1", b"<script>", mime)
    assert response.media_type == "application/octet-stream"
    assert response.headers["content-disposition"] == 'attachment; filename="f1"'
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]


@pytest.mark.parametrize("mime", ["image/png", "audio/mpeg", "video/mp4", "text/plain"])
def test_passive_types_stay_inline(mime):
    response = _download_response("f1", b"x", mime)
    assert response.media_type == mime
    assert response.headers["content-disposition"] == "inline"
    assert "sandbox" in response.headers["content-security-policy"]


def test_pdf_is_inline_without_the_sandbox_csp():
    response = _download_response("f1", b"%PDF", "application/pdf")
    assert response.headers["content-disposition"] == "inline"
    assert "content-security-policy" not in response.headers


def test_other_types_download_with_their_type():
    docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    response = _download_response("f1", b"PK", docx)
    assert response.media_type == docx
    assert response.headers["content-disposition"].startswith("attachment")


def test_missing_type_is_opaque():
    assert _download_response("f1", b"x", None).media_type == "application/octet-stream"


# ---------------------------------------------------------------------------
# L4: caches are bounded
# ---------------------------------------------------------------------------


def test_cache_evicts_least_recently_used_past_the_entry_limit():
    cache = _BoundedCache(max_entries=2)
    cache["a"], cache["b"] = 1, 2
    cache.get("a")
    cache["c"] = 3
    assert list(cache) == ["a", "c"]


def test_cache_evicts_past_the_character_budget():
    cache = _BoundedCache(max_entries=100, max_chars=10)
    for key in "abcd":
        cache[key] = "x" * 4
    assert list(cache) == ["c", "d"]
    assert cache._chars == 8


def test_cache_tracks_size_on_overwrite_and_pop():
    cache = _BoundedCache(max_entries=10, max_chars=100)
    cache["a"] = "x" * 5
    cache["a"] = "x" * 2
    assert cache._chars == 2
    cache.pop("a")
    assert cache._chars == 0


# ---------------------------------------------------------------------------
# L6 / L8: config endpoint and storage types
# ---------------------------------------------------------------------------


def test_multimodal_config_does_not_expose_the_storage_path():
    assert "media_storage_path" not in MultimodalConfigResponse.model_fields


def test_unimplemented_pg_storage_is_rejected_at_startup():
    with pytest.raises(ValidationError):
        MediaSettings(MEDIA_STORAGE_TYPE="pg", _env_file=None)
