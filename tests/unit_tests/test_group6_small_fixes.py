"""G-161 (checkpointer key warning), G-169 (stale graph:setup scope)."""

# ruff: noqa: S101

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tenxgraph_api.cli.config_editor.schema import AUTHORIZATION_SCOPES
from tenxgraph_api.src.app import loader


@pytest.mark.asyncio
async def test_checkpointer_key_logs_a_warning(caplog):
    config = MagicMock()
    config.remote_tools = []
    config.graph_path = "graph:app"
    config.checkpointer_path = "graph.deps:cp"
    with (
        patch.object(loader, "load_graph", AsyncMock(return_value=None)),
        patch.object(loader, "load_store", return_value=None),
        patch.object(loader, "load_container", return_value=None),
        caplog.at_level(logging.WARNING, logger="tenxgraph_api"),
    ):
        try:
            await loader.attach_all_modules(config, MagicMock())
        except Exception:  # - only the warning matters here
            pass
    assert any('"checkpointer" key' in r.getMessage() for r in caplog.records)


def test_skill_templates_do_not_tell_developers_to_set_the_checkpointer_key():
    root = Path(loader.__file__).parents[2] / "cli" / "templates" / "skills"
    for name in ("api-configuration.md", "dependency-injection.md"):
        text = (root / "10xgraph" / "references" / name).read_text(encoding="utf-8")
        assert '"checkpointer":' not in text, name


def test_setup_scope_is_gone_from_the_editor():
    assert "graph:setup" not in AUTHORIZATION_SCOPES


# ---------------------------------------------------------------------------
# G-119
# ---------------------------------------------------------------------------


def test_allowed_host_list_is_stripped_for_the_middleware(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tenxgraph_api.src.app.core.config import setup_middleware as module
    from tenxgraph_api.src.app.core.config.settings import Settings

    settings = Settings(MODE="development", ALLOWED_HOST="testserver, example.com ", _env_file=None)
    monkeypatch.setattr(module, "get_settings", lambda: settings, raising=False)
    app = FastAPI()
    with patch.object(module, "settings", settings, create=True):
        module.setup_middleware(app)

    @app.get("/x")
    def x():
        return {"ok": True}

    assert TestClient(app, base_url="http://example.com").get("/x").status_code == 200


def test_mode_sets_app_debug():
    import inspect

    from tenxgraph_api.src.app import main

    assert 'debug=settings.MODE != "production"' in inspect.getsource(main)
    assert main.app.debug is (main.settings.MODE != "production")


def test_production_warns_about_debug_logging_and_full_otel(caplog):
    from tenxgraph_api.src.app.core.config.settings import Settings

    with caplog.at_level(logging.WARNING):
        Settings(
            MODE="production",
            LOG_LEVEL="DEBUG",
            OTEL_ENABLED=True,
            OTEL_LEVEL="full",
            ORIGINS="https://a.example",
            ALLOWED_HOST="a.example",
            _env_file=None,
        )
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "LOG_LEVEL=DEBUG" in text
    assert "OTEL_LEVEL=full" in text


def test_dev_command_warns_about_reload_and_open_host_in_production():
    from tenxgraph_api.cli.commands.api import APICommand

    warn = APICommand.production_launch_warnings
    assert len(warn("production", "0.0.0.0", True)) == 2
    assert warn("development", "0.0.0.0", True) == []
    assert warn("production", "127.0.0.1", False) == []


def test_star_role_is_not_granted_the_removed_setup_scope():
    from tenxgraph_api.src.app.core.auth.authorization import all_scopes

    assert "graph:setup" not in all_scopes()
