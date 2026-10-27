"""MODE is normalised to ``production`` / ``development``; unknown values refuse to start."""

# ruff: noqa: S101

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from tenxgraph_api.src.app.core.auth.route_guard import _iter_routes
from tenxgraph_api.src.app.core.config.settings import Settings
from tenxgraph_api.src.app.routers.setup_router import init_routes


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("MODE", "DOCS_PATH", "REDOCS_PATH"):
        monkeypatch.delenv(name, raising=False)
    # Production refuses wildcard hosts/origins (strict checks default on); these tests are
    # about MODE, so give them safe values.
    monkeypatch.setenv("ALLOWED_HOST", "api.example.com")
    monkeypatch.setenv("ORIGINS", "https://app.example.com")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("production", "production"),
        ("prod", "production"),
        ("PROD", "production"),
        (" Production ", "production"),
        ("development", "development"),
        ("dev", "development"),
        ("DEV", "development"),
        ("local", "development"),
        ("", "development"),
    ],
)
def test_aliases_are_normalised(raw, expected):
    mode = Settings(MODE=raw, _env_file=None).MODE
    assert mode == expected


def test_aliases_from_environment(monkeypatch):
    monkeypatch.setenv("MODE", " Prod ")
    assert Settings(_env_file=None).MODE == "production"


def test_unset_defaults_to_development():
    assert Settings(_env_file=None).MODE == "development"


@pytest.mark.parametrize("raw", ["prodution", "staging", "test"])
def test_unknown_value_refuses_to_start(raw):
    with pytest.raises(ValidationError) as exc:
        Settings(MODE=raw, _env_file=None)
    message = str(exc.value)
    assert "Invalid MODE" in message
    for accepted in ("production", "prod", "development", "dev", "local"):
        assert accepted in message


def test_production_alias_turns_on_production_behaviour():
    settings = Settings(MODE="prod", _env_file=None)
    assert settings.DOCS_PATH == ""
    assert settings.REDOCS_PATH == ""
    # The comparison main.py uses to leave the evals router out.
    app = FastAPI()
    init_routes(app, evals=settings.MODE != "production")
    paths = {path for path, _route, _guarded in _iter_routes(app.routes)}
    assert not any(path.startswith("/v1/evals") for path in paths)
