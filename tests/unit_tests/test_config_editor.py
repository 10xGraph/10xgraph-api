"""Tests for the ``10xgraph config`` browser editor backend."""

from __future__ import annotations

import http.client
import importlib.util
import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

import tenxgraph_api.cli.main as main_mod
from tenxgraph_api.cli.config_editor import (
    ConfigConflictError,
    ConfigEditorServer,
    ConfigFileStore,
    build_schema,
    validate_config,
)
from tenxgraph_api.cli.config_editor.schema import AUTHORIZATION_SCOPES, KNOWN_TOP_LEVEL_KEYS
from tenxgraph_api.cli.config_editor.server import TOKEN_HEADER
from tenxgraph_api.src.app.core.config.graph_config import DEFAULT_USER_REVOCATION_TTL


def _levels(issues: list[dict], path: str) -> list[str]:
    return [issue["level"] for issue in issues if issue["path"] == path]


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


def test_schema_is_json_and_fields_live_under_their_section() -> None:
    schema = json.loads(json.dumps(build_schema()))
    ids = [section["id"] for section in schema["sections"]]
    assert len(ids) == len(set(ids))
    for section in schema["sections"]:
        for field in section["fields"]:
            if section["key"] is not None:
                assert field["path"][0] == section["key"]
            assert field["path"][0] in KNOWN_TOP_LEVEL_KEYS


def test_schema_scopes_match_api_scopes() -> None:
    from tenxgraph_api.src.app.core.auth.authorization import all_scopes

    assert set(AUTHORIZATION_SCOPES) == set(all_scopes())


def _fields() -> dict[str, dict]:
    return {
        ".".join(field["path"]): field
        for section in build_schema()["sections"]
        for field in section["fields"]
    }


def _env_entries() -> list[dict]:
    schema = build_schema()
    entries = [e for section in schema["sections"] for e in section.get("env", [])]
    entries += [v for group in schema["environment"] for v in group["vars"]]
    return entries


def test_env_entries_have_the_documented_shape() -> None:
    schema = build_schema()
    assert schema["environment"]
    for group in schema["environment"]:
        assert set(group) <= {"title", "vars", "note"}
        assert {"title", "vars"} <= set(group)
        assert group["title"] and group["vars"]
        if "note" in group:
            assert isinstance(group["note"], str) and group["note"].strip()
            assert "\u2014" not in group["note"]
    snowflake = next(g for g in schema["environment"] if "Snowflake" in g["title"])
    assert snowflake["note"]
    entries = _env_entries()
    assert entries
    for entry in entries:
        assert set(entry) == {"name", "purpose"}
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", entry["name"])
        assert entry["purpose"].strip()
        assert "\u2014" not in entry["purpose"]


def test_every_env_name_exists_in_the_source_tree() -> None:
    root = Path(main_mod.__file__).resolve().parents[1]
    sources = [
        p for p in root.rglob("*.py") if p.name != "schema.py" or "config_editor" not in str(p)
    ]
    # Core env names live in the installed tenxgraph package (a sibling checkout only exists in
    # the monorepo, not in this repo's CI).
    core_spec = importlib.util.find_spec("tenxgraph")
    for location in (core_spec.submodule_search_locations or []) if core_spec else []:
        sources += list(Path(location).rglob("*.py"))
    text = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in sources)
    missing = sorted({e["name"] for e in _env_entries() if e["name"] not in text})
    assert not missing


def test_tri_state_fields_have_no_default() -> None:
    fields = _fields()
    for path in (
        "graph_introspection",
        "websocket.enabled",
        "observability.logfire.console",
    ):
        assert fields[path]["unset_label"]
        assert "default" not in fields[path]


def test_logfire_console_does_not_offer_true() -> None:
    assert _fields()["observability.logfire.console"]["allow_true"] is False


def test_websocket_seed_does_not_force_enabled() -> None:
    section = next(s for s in build_schema()["sections"] if s["id"] == "websocket")
    assert "enabled" not in section["default"]
    issues = validate_config({"agent": "a:b", "websocket": section["default"]})
    assert not [i for i in issues if i["path"].startswith("websocket")]


def test_number_field_extensions() -> None:
    fields = _fields()
    timeout = fields["runs.timeout"]
    assert timeout["exclusive_min"] == 0
    assert timeout["null_label"] == "No limit"
    assert "min" not in timeout
    for path in (
        "runs.max_concurrent_per_user",
        "websocket.max_connections",
        "websocket.max_connections_per_user",
    ):
        assert fields[path]["unlimited_value"] == 0


def test_rate_limit_seed_does_not_claim_a_server_default_for_excluded_paths() -> None:
    section = next(s for s in build_schema()["sections"] if s["id"] == "rate_limit")
    assert "exclude_paths" not in section["default"]
    assert _fields()["rate_limit.exclude_paths"]["label"] == "Extra paths to skip"


def test_rate_limit_has_no_backend_fields_and_the_cache_store_is_wired() -> None:
    fields = _fields()
    assert not [
        path for path in fields if path.startswith(("rate_limit.backend", "rate_limit.redis"))
    ]
    rate_limit = next(s for s in build_schema()["sections"] if s["id"] == "rate_limit")
    assert "backend" not in rate_limit["default"]
    assert fields["cache_store"]["group"] == fields["redis"]["group"] == "Cache store"
    assert "cache_store" in KNOWN_TOP_LEVEL_KEYS


def test_token_revocation_section_writes_true_and_takes_its_settings() -> None:
    section = next(s for s in build_schema()["sections"] if s["id"] == "token_revocation")
    assert section["key"] == "token_revocation"
    assert section["default"] is True
    paths = [".".join(f["path"]) for f in section["fields"]]
    assert paths == [
        "token_revocation.enabled",
        "token_revocation.token_lifetime",
        "token_revocation.user_ttl",
    ]
    assert _fields()["token_revocation.user_ttl"]["default"] == DEFAULT_USER_REVOCATION_TTL
    config = {"agent": "a:b", "auth": "jwt", "redis": "redis://x"}
    for value in (True, {"token_lifetime": 3600}, {"enabled": True, "user_ttl": None}):
        issues = validate_config({**config, "token_revocation": value})
        assert not [i for i in issues if i["path"].startswith("token_revocation")]


def test_redis_key_expands_env_vars_and_warns_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("AF_TEST_REDIS_URL", raising=False)
    issues = validate_config({"agent": "a:b", "redis": "${AF_TEST_REDIS_URL}"})
    assert _levels(issues, "redis") == ["warning"]
    monkeypatch.setenv("AF_TEST_REDIS_URL", "redis://x")
    assert (
        _levels(validate_config({"agent": "a:b", "redis": "${AF_TEST_REDIS_URL}"}), "redis") == []
    )


def test_cache_store_must_be_an_import_path() -> None:
    assert _levels(validate_config({"agent": "a:b", "cache_store": "nope"}), "cache_store") == [
        "error"
    ]
    assert (
        _levels(validate_config({"agent": "a:b", "cache_store": "pkg.mod:store"}), "cache_store")
        == []
    )


def test_unlimited_values_are_accepted_by_the_server() -> None:
    for value in (0, None):
        config = {
            "agent": "a:b",
            "runs": {"max_concurrent_per_user": value},
            "websocket": {"max_connections": value, "max_connections_per_user": value},
        }
        assert not [i for i in validate_config(config) if i["level"] == "error"]


@pytest.mark.parametrize(("timeout", "ok"), [(0, False), (-1, False), (1.5, True), (None, True)])
def test_runs_timeout_validation(timeout: object, ok: bool) -> None:
    issues = validate_config({"agent": "a:b", "runs": {"timeout": timeout}})
    assert (not _levels(issues, "runs")) is ok


@pytest.mark.parametrize(("console", "ok"), [(False, True), (None, True), (True, False)])
def test_logfire_console_only_accepts_false(console: object, ok: bool) -> None:
    config = {"agent": "a:b", "observability": {"logfire": {"enabled": True, "console": console}}}
    issues = validate_config(config)
    assert (not _levels(issues, "observability.logfire.console")) is ok


def test_build_schema_returns_independent_copies() -> None:
    first = build_schema()
    first["sections"][0]["title"] = "changed"
    assert build_schema()["sections"][0]["title"] != "changed"


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_minimal_config_has_no_errors() -> None:
    issues = validate_config({"agent": "graph.react:app", "env": ".env"})
    assert not [issue for issue in issues if issue["level"] == "error"]


@pytest.mark.parametrize("config", [{}, {"agent": ""}])
def test_agent_is_required(config: dict) -> None:
    assert _levels(validate_config(config), "agent") == ["error"]


def test_non_object_config_is_rejected() -> None:
    assert validate_config([])[0]["level"] == "error"


@pytest.mark.parametrize("value", ["graph.react", "graph react:app", 42])
def test_agent_must_be_an_import_path(value: object) -> None:
    assert _levels(validate_config({"agent": value}), "agent") == ["error"]


@pytest.mark.parametrize(
    ("auth", "path", "expected"),
    [
        ("jwt", "auth", []),
        ("basic", "auth", ["error"]),
        ({"method": "custom", "path": "auth.backend:Auth"}, "auth.path", []),
        ({"method": "custom"}, "auth.path", ["error"]),
        ({"method": "jwt", "path": "x:y"}, "auth.method", ["error"]),
        (123, "auth", ["error"]),
    ],
)
def test_auth_variants(auth: object, path: str, expected: list[str]) -> None:
    issues = validate_config({"agent": "graph.react:app", "auth": auth})
    assert _levels(issues, path) == expected


@pytest.mark.parametrize(
    ("authorization", "path", "expected"),
    [
        ("ownership", "authorization", []),
        ("ALLOW_ALL", "authorization", []),
        ("auth.perm:Backend", "authorization", []),
        ("everyone", "authorization", ["error"]),
        ({"backend": "acl"}, "authorization.backend", ["error"]),
        ({"backend": "rbac", "roles": {"admin": "*"}}, "authorization.roles.admin", ["error"]),
        (
            {"backend": "rbac", "roles": {"a": []}, "isolation": "x"},
            "authorization.isolation",
            ["error"],
        ),
        ({"backend": "rbac", "roles": {}}, "authorization.roles", ["warning"]),
        ({"type": "rbac", "role_scopes": {"a": ["graph:read"]}}, "authorization.roles", []),
    ],
)
def test_authorization_variants(authorization: object, path: str, expected: list[str]) -> None:
    issues = validate_config({"agent": "graph.react:app", "authorization": authorization})
    assert _levels(issues, path) == expected


def test_rate_limit_uses_server_parser() -> None:
    issues = validate_config({"agent": "graph.react:app", "rate_limit": {"by": "country"}})
    assert _levels(issues, "rate_limit") == ["error"]
    assert "rate_limit.by" in next(i["message"] for i in issues if i["path"] == "rate_limit")


@pytest.mark.parametrize("removed", [{"backend": "memory"}, {"redis": {"url": "redis://x"}}])
def test_removed_rate_limit_keys_are_errors_naming_the_cache_store(removed) -> None:
    issues = validate_config({"agent": "graph.react:app", "rate_limit": removed})
    assert _levels(issues, "rate_limit") == ["error"]
    assert "cache_store" in next(i["message"] for i in issues if i["path"] == "rate_limit")


def test_websocket_rejects_negative_limit() -> None:
    issues = validate_config({"agent": "graph.react:app", "websocket": {"max_connections": -1}})
    assert _levels(issues, "websocket") == ["error"]


def test_ag_ui_is_known_and_validated() -> None:
    ok = validate_config({"agent": "graph.react:app", "ag_ui": {"enabled": True}})
    assert _levels(ok, "ag_ui") == []
    bad = validate_config({"agent": "graph.react:app", "ag_ui": {"enabled": "maybe"}})
    assert _levels(bad, "ag_ui") == ["error"]


def test_observability_checks() -> None:
    issues = validate_config(
        {"agent": "graph.react:app", "observability": {"level": "all", "logfire": []}}
    )
    assert _levels(issues, "observability.level") == ["error"]
    assert _levels(issues, "observability.logfire") == ["error"]
    assert _levels(issues, "observability") == ["warning"]


def test_observability_user_id_mode() -> None:
    for mode in ("raw", "hash", "mask"):
        ok = validate_config({"agent": "graph.react:app", "observability": {"user_id_mode": mode}})
        assert _levels(ok, "observability.user_id_mode") == []
    bad = validate_config({"agent": "graph.react:app", "observability": {"user_id_mode": "omit"}})
    assert _levels(bad, "observability.user_id_mode") == ["error"]


def test_remote_tools_duplicates_are_errors() -> None:
    tool = {"node": "MAIN", "name": "locate", "description": "Find the user"}
    issues = validate_config({"agent": "graph.react:app", "remote_tools": [tool, tool]})
    assert _levels(issues, "remote_tools") == ["error"]


def test_test_and_evaluation_ranges() -> None:
    issues = validate_config(
        {
            "agent": "graph.react:app",
            "test": {"coverage": "yes", "coverage_threshold": 120},
            "evaluation": {"threshold": 2, "max_concurrency": 0, "parallel": 1},
        }
    )
    for path in (
        "test.coverage",
        "test.coverage_threshold",
        "evaluation.threshold",
        "evaluation.max_concurrency",
        "evaluation.parallel",
    ):
        assert _levels(issues, path) == ["error"], path


def test_unknown_keys_are_warnings_on_core_section() -> None:
    issues = validate_config({"agent": "graph.react:app", "routers": {}})
    [issue] = [i for i in issues if i["path"] == "routers"]
    assert issue["level"] == "warning"
    assert issue["section"] == "core"


def test_plugins_and_checkpointer_warning() -> None:
    issues = validate_config(
        {"agent": "graph.react:app", "store": "not a path", "checkpointer": "graph.deps:cp"}
    )
    assert _levels(issues, "store") == ["error"]
    assert _levels(issues, "checkpointer") == ["warning"]


def test_file_checks_use_base_dir(tmp_path: Path) -> None:
    (tmp_path / "graph").mkdir()
    (tmp_path / "graph" / "react.py").write_text("app = None\n")
    config = {"agent": "graph.react:app", "env": ".env", "store": "graph.missing:store"}
    issues = validate_config(config, tmp_path)
    assert _levels(issues, "agent") == []
    assert _levels(issues, "env") == ["warning"]
    assert _levels(issues, "store") == ["warning"]


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


def test_store_creates_new_file_without_backup(tmp_path: Path) -> None:
    store = ConfigFileStore(tmp_path / "10xgraph.json")
    loaded = store.load()
    assert loaded.config is None and loaded.version is None

    version = store.save({"agent": "graph.react:app"}, None)
    assert store.load().version == version
    assert json.loads(store.path.read_text()) == {"agent": "graph.react:app"}
    assert not store.backup_path.exists()


def test_store_keeps_key_order_and_backup(tmp_path: Path) -> None:
    path = tmp_path / "10xgraph.json"
    path.write_text(json.dumps({"env": ".env", "agent": "a:b", "custom": 1}))
    store = ConfigFileStore(path)
    version = store.load().version

    store.save({"agent": "a:c", "auth": "jwt", "env": ".env", "custom": 1}, version)

    assert list(json.loads(path.read_text())) == ["env", "agent", "custom", "auth"]
    assert json.loads(store.backup_path.read_text())["agent"] == "a:b"
    assert path.read_text().endswith("}\n")


def test_store_detects_concurrent_edits(tmp_path: Path) -> None:
    path = tmp_path / "10xgraph.json"
    path.write_text('{"agent": "a:b"}')
    store = ConfigFileStore(path)
    version = store.load().version
    path.write_text('{"agent": "x:y"}')
    with pytest.raises(ConfigConflictError):
        store.save({"agent": "a:c"}, version)
    with pytest.raises(ConfigConflictError):
        ConfigFileStore(tmp_path / "other.json").save({"agent": "a:b"}, "stale")


@pytest.mark.parametrize("content", ["{broken", "[1, 2]"])
def test_store_reports_unparseable_files(tmp_path: Path, content: str) -> None:
    path = tmp_path / "10xgraph.json"
    path.write_text(content)
    loaded = ConfigFileStore(path).load()
    assert loaded.config is None
    assert loaded.version is not None
    assert loaded.parse_error


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


@pytest.fixture
def editor(tmp_path: Path):
    path = tmp_path / "10xgraph.json"
    path.write_text(json.dumps({"agent": "graph.react:app", "custom": True}))
    server = ConfigEditorServer(path, token="secret-token")
    server.start_in_background()
    yield server
    server.shutdown()


def _request(
    server: ConfigEditorServer,
    method: str,
    route: str,
    body: object = None,
    *,
    token: str | None = "secret-token",  # noqa: S107
    host: str | None = None,
    content_type: str = "application/json",
) -> tuple[int, dict | str]:
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{server.port}"}
    if token is not None:
        headers[TOKEN_HEADER] = token
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = content_type
    conn.request(method, route, body=payload, headers=headers)
    response = conn.getresponse()
    raw = response.read().decode()
    conn.close()
    if response.getheader("Content-Type", "").startswith("application/json"):
        return response.status, json.loads(raw)
    return response.status, raw


def test_server_serves_page_without_token(editor: ConfigEditorServer) -> None:
    status, body = _request(editor, "GET", "/", token=None)
    assert status == 200
    assert "10xGraph Config" in body
    assert editor.url.endswith("#token=secret-token")


@pytest.mark.parametrize(
    ("route", "content_type"),
    [("/app.js", "text/javascript"), ("/app.css", "text/css"), ("/favicon.svg", "image/svg+xml")],
)
def test_server_serves_built_assets(
    editor: ConfigEditorServer, route: str, content_type: str
) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", editor.port, timeout=5)
    conn.request("GET", route, headers={"Host": f"127.0.0.1:{editor.port}"})
    response = conn.getresponse()
    body = response.read()
    conn.close()
    assert response.status == 200
    assert response.getheader("Content-Type", "").startswith(content_type)
    assert body


def test_server_requires_token_and_loopback_host(editor: ConfigEditorServer) -> None:
    assert _request(editor, "GET", "/api/state", token=None)[0] == 403
    assert _request(editor, "GET", "/api/state", token="wrong")[0] == 403
    assert _request(editor, "GET", "/api/state", host="evil.example:80")[0] == 403
    assert _request(editor, "GET", "/nope")[0] == 404


def test_server_state(editor: ConfigEditorServer) -> None:
    status, body = _request(editor, "GET", "/api/state")
    assert status == 200
    assert body["exists"] is True
    assert body["config"] == {"agent": "graph.react:app", "custom": True}
    assert body["schema"]["sections"]
    assert any(issue["path"] == "custom" for issue in body["issues"])


def test_server_state_for_missing_file(tmp_path: Path) -> None:
    server = ConfigEditorServer(tmp_path / "10xgraph.json", token="t")
    server.start_in_background()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("GET", "/api/state", headers={TOKEN_HEADER: "t"})
        body = json.loads(conn.getresponse().read())
        conn.close()
    finally:
        server.shutdown()
    assert body["exists"] is False
    assert body["config"]["agent"]


def test_server_validate(editor: ConfigEditorServer) -> None:
    status, body = _request(editor, "POST", "/api/validate", {"config": {"agent": ""}})
    assert status == 200
    assert any(issue["path"] == "agent" for issue in body["issues"])
    status, _ = _request(editor, "POST", "/api/validate", {"config": {}}, content_type="text/plain")
    assert status == 415


def test_server_save_flow(editor: ConfigEditorServer) -> None:
    _, state = _request(editor, "GET", "/api/state")
    config = {**state["config"], "auth": "jwt"}

    status, body = _request(
        editor, "POST", "/api/save", {"config": {}, "version": state["version"]}
    )
    assert status == 422

    status, body = _request(editor, "POST", "/api/save", {"config": config, "version": "stale"})
    assert status == 409

    status, body = _request(
        editor, "POST", "/api/save", {"config": config, "version": state["version"]}
    )
    assert status == 200
    assert body["backup"]
    assert json.loads(editor.store.path.read_text()) == config
    assert list(body["config"]) == ["agent", "custom", "auth"]


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_config_command_delegates(monkeypatch) -> None:
    called: dict = {}

    def fake_execute(self, **kwargs):
        called.update(kwargs)
        return 0

    monkeypatch.setattr(main_mod.ConfigCommand, "execute", fake_execute)
    result = CliRunner().invoke(main_mod.app, ["config", "--no-open", "-c", "x.json", "-p", "9"])
    assert result.exit_code == 0
    assert called == {"config": "x.json", "port": 9, "open_browser": False}


def test_config_command_runs_until_interrupted(monkeypatch, tmp_path: Path) -> None:
    from tenxgraph_api.cli.commands import config as config_cmd

    opened: list[str] = []

    def interrupt(self) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(config_cmd.ConfigEditorServer, "serve_forever", interrupt)
    monkeypatch.setattr(config_cmd.webbrowser, "open_new_tab", opened.append)

    command = config_cmd.ConfigCommand()
    assert command.execute(config=str(tmp_path / "10xgraph.json"), open_browser=True) == 0
    assert len(opened) == 1
    assert "#token=" in opened[0]


def test_favicon_matches_the_docs_site() -> None:
    from importlib.resources import files

    docs_icon = Path(__file__).resolve().parents[3] / "agentflow-docs" / "public" / "favicon.svg"
    if not docs_icon.is_file():
        pytest.skip("agentflow-docs is not checked out next to this package")
    served = files("tenxgraph_api.cli.config_editor").joinpath("static", "favicon.svg")
    assert served.read_bytes() == docs_icon.read_bytes()


def test_store_seeds_a_new_file_from_the_legacy_one(tmp_path: Path) -> None:
    legacy = tmp_path / "agentflow.json"
    legacy.write_text(json.dumps({"env": ".env", "agent": "old:app"}))
    store = ConfigFileStore(tmp_path / "10xgraph.json", seed_path=legacy)

    loaded = store.load()
    assert loaded.config == {"env": ".env", "agent": "old:app"}
    assert loaded.version is None  # target does not exist yet
    assert store.seeded

    store.save({"agent": "new:app", "env": ".env"}, None)

    assert list(json.loads(store.path.read_text())) == ["env", "agent"]  # seed's key order
    assert json.loads(store.path.read_text())["agent"] == "new:app"
    assert json.loads(legacy.read_text())["agent"] == "old:app"  # legacy file untouched
    assert not store.seeded
    assert store.load().config == {"env": ".env", "agent": "new:app"}


def test_store_ignores_an_unparseable_seed(tmp_path: Path) -> None:
    legacy = tmp_path / "agentflow.json"
    legacy.write_text("{broken")
    store = ConfigFileStore(tmp_path / "10xgraph.json", seed_path=legacy)
    assert store.load().config is None
    assert not store.seeded


def test_config_command_targets_10xgraph_json_when_only_legacy_exists(
    monkeypatch, tmp_path: Path
) -> None:
    from tenxgraph_api.cli.commands import config as config_cmd

    (tmp_path / "agentflow.json").write_text(json.dumps({"agent": "old:app"}))
    servers: list = []
    original_init = config_cmd.ConfigEditorServer.__init__

    def capture(self, *args, **kwargs) -> None:
        original_init(self, *args, **kwargs)
        servers.append(self)

    def interrupt(self) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(config_cmd.ConfigEditorServer, "__init__", capture)
    monkeypatch.setattr(config_cmd.ConfigEditorServer, "serve_forever", interrupt)
    monkeypatch.chdir(tmp_path)

    assert config_cmd.ConfigCommand().execute(open_browser=False) == 0
    store = servers[0].store
    assert store.path == tmp_path / "10xgraph.json"
    assert store.seed_path == tmp_path / "agentflow.json"
    assert store.load().config == {"agent": "old:app"}
