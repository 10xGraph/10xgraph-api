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
from tenxgraph_api.cli.config_editor.schema import (
    KNOWN_TOP_LEVEL_KEYS,
    section_for_key,
)
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


def test_schema_has_an_auth_tab_and_no_authorization_entry() -> None:
    sections = {section["id"]: section for section in build_schema()["sections"]}
    assert "authorization" not in sections
    assert sections["auth"]["widget"] == "auth"
    assert sections["auth"]["key"] == "auth"


def test_schema_serves_the_permission_registry() -> None:
    from tenxgraph_api.src.app.core.auth.permissions_registry import REGISTRY

    registry = {s["id"]: s for s in build_schema()["sections"]}["auth"]["registry"]
    assert registry == {resource: list(actions) for resource, actions in REGISTRY.items()}


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


def _section(section_id: str) -> dict:
    return next(s for s in build_schema()["sections"] if s["id"] == section_id)


def test_sections_for_the_cache_store_and_wiring_replace_plugins() -> None:
    ids = [s["id"] for s in build_schema()["sections"]]
    for removed in ("rate_limit", "token_revocation", "plugins"):
        assert removed not in ids
    for present in (
        "cache_store",
        "checkpointer",
        "id_generator",
        "thread_name_generator",
        "store",
        "injectq",
    ):
        assert present in ids
    assert _section("cache_store")["subtitle"] == "rate limiting, token revocation"
    assert not {"redis", "rate_limit", "token_revocation"} & KNOWN_TOP_LEVEL_KEYS
    assert "id_generator" in KNOWN_TOP_LEVEL_KEYS


def test_cache_store_fields_sit_under_cache_store() -> None:
    fields = _fields()
    assert {"cache_store.type", "cache_store.url", "cache_store.path"} <= set(fields)
    assert "REDIS_URL" in fields["cache_store.url"]["placeholder"]
    assert fields["cache_store.type"]["options"] == ["memory", "redis", "custom"]
    rate_limit = [p for p in fields if p.startswith("cache_store.rate_limit.")]
    assert sorted(p.rsplit(".", 1)[1] for p in rate_limit) == sorted(
        [
            "requests",
            "window",
            "by",
            "fail_open",
            "trusted_proxy_headers",
            "trusted_proxy_hops",
            "trusted_proxies",
            "exclude_paths",
        ]
    )
    assert fields["cache_store.rate_limit.exclude_paths"]["label"] == "Extra paths to skip"
    assert fields["cache_store.token_revocation.user_ttl"]["default"] == DEFAULT_USER_REVOCATION_TTL
    assert all(fields[p].get("managed") for p in fields if p.startswith("cache_store."))
    for text in (f["help"] for f in fields.values() if "help" in f):
        assert "Plugins" not in text


def test_choice_sections_mirror_the_server_names() -> None:
    from tenxgraph_api.cli.config_editor.schema import ID_GENERATOR_NAMES, THREAD_NAME_CHOICES
    from tenxgraph_api.src.app.core.config import graph_config

    assert ID_GENERATOR_NAMES == graph_config.ID_GENERATOR_NAMES
    assert THREAD_NAME_CHOICES == graph_config.THREAD_NAME_CHOICES
    assert [c[0] for c in _section("id_generator")["choices"]] == list(ID_GENERATOR_NAMES)
    assert [c[0] for c in _section("thread_name_generator")["choices"]] == list(THREAD_NAME_CHOICES)


def test_checkpointer_section_lists_its_environment() -> None:
    names = [e["name"] for e in _section("checkpointer").get("env", [])]
    assert names == ["DATABASE_URL", "REDIS_URL"]


def test_cache_store_values_the_editor_writes_are_valid() -> None:
    base = {"agent": "a:b", "auth": "jwt"}
    for cache_store in (
        {},
        {"type": "memory"},
        {"type": "redis", "url": "redis://x", "token_revocation": True},
        {"type": "custom", "path": "pkg.mod:store"},
        {
            "type": "redis",
            "url": "redis://x",
            "rate_limit": {"requests": 100, "window": 60, "by": "ip"},
            "token_revocation": {"enabled": True, "token_lifetime": 3600, "user_ttl": None},
        },
    ):
        issues = validate_config({**base, "cache_store": cache_store})
        assert not [i for i in issues if i["level"] == "error"], cache_store


def test_cache_store_type_errors_point_at_the_field() -> None:
    base = {"agent": "a:b"}
    # No url is fine (the server uses REDIS_URL); only a bad one is an error.
    assert _levels(
        validate_config({**base, "cache_store": {"type": "redis", "url": ""}}), "cache_store.url"
    ) == ["error"]
    assert _levels(
        validate_config({**base, "cache_store": {"type": "custom"}}), "cache_store.path"
    ) == ["error"]
    assert _levels(
        validate_config({**base, "cache_store": {"type": "memory", "url": "redis://x"}}),
        "cache_store.url",
    ) == ["error"]
    assert _levels(validate_config({**base, "cache_store": {"type": "x"}}), "cache_store.type") == [
        "error"
    ]
    assert _levels(validate_config({**base, "cache_store": "nope"}), "cache_store") == ["error"]


def test_cache_store_custom_path_must_be_an_import_path() -> None:
    config = {"agent": "a:b", "cache_store": {"type": "custom", "path": "pkg.mod:store"}}
    assert _levels(validate_config(config), "cache_store.path") == []


def test_cache_store_url_expands_env_vars_and_warns_when_unset(monkeypatch) -> None:
    config = {"agent": "a:b", "cache_store": {"type": "redis", "url": "${AF_TEST_REDIS_URL}"}}
    monkeypatch.delenv("AF_TEST_REDIS_URL", raising=False)
    assert _levels(validate_config(config), "cache_store.url") == ["warning"]
    monkeypatch.setenv("AF_TEST_REDIS_URL", "redis://x")
    assert _levels(validate_config(config), "cache_store.url") == []


@pytest.mark.parametrize("key", ["redis", "rate_limit", "token_revocation"])
def test_settings_that_belong_in_cache_store_are_errors_at_the_top_level(key: str) -> None:
    issues = validate_config({"agent": "a:b", key: {} if key != "redis" else "redis://x"})
    assert _levels(issues, key) == ["error"]
    assert "cache_store" in next(i["message"] for i in issues if i["path"] == key)


def test_rate_limit_inside_cache_store_uses_server_parser() -> None:
    config = {"agent": "a:b", "cache_store": {"rate_limit": {"by": "country"}}}
    issues = validate_config(config)
    assert _levels(issues, "cache_store") == ["error"]
    config = {"agent": "a:b", "cache_store": {"rate_limit": {"backend": "memory"}}}
    issues = validate_config(config)
    assert "unknown keys" in next(i["message"] for i in issues if i["path"] == "cache_store")


@pytest.mark.parametrize(
    ("key", "good", "bad"),
    [
        ("id_generator", "snowflake", "nope"),
        ("thread_name_generator", "first_message", "nope"),
    ],
)
def test_choice_keys_take_a_name_or_an_import_path(key: str, good: str, bad: str) -> None:
    base = {"agent": "a:b"}
    assert _levels(validate_config({**base, key: good}), key) == []
    assert _levels(validate_config({**base, key: "pkg.mod:thing"}), key) == []
    assert _levels(validate_config({**base, key: ""}), key) == []
    assert _levels(validate_config({**base, key: bad}), key) == ["error"]


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


RBAC = {
    "method": "jwt",
    "roles_claim": "roles",
    "tenant_claims": ["org_id"],
    "roles": {
        "member": {"permissions": ["graph:*", "threads:read"], "reach": "own"},
        "org_admin": {"inherits": ["member"], "permissions": ["admin:read"], "reach": "tenant"},
    },
    "default_role": "member",
    "system_identities": {"nightly": {"roles": ["member"], "tenant": {"org_id": "acme"}}},
}


def _auth_issues(auth: object, **extra: object) -> list[dict]:
    return validate_config({"agent": "graph.react:app", "auth": auth, **extra})


@pytest.mark.parametrize(
    ("auth", "path", "expected"),
    [
        ("jwt", "auth", []),
        (None, "auth", ["warning"]),
        ({"method": "none"}, "auth", ["warning"]),
        ("basic", "auth", ["error"]),
        ({"method": "custom", "path": "auth.backend:Auth"}, "auth.path", []),
        ({"method": "custom"}, "auth.path", ["error"]),
        ({"method": "jwt", "path": "x:y"}, "auth.path", ["error"]),
        ({"method": "none", "roles_claim": "roles"}, "auth", ["error", "warning"]),
        (123, "auth", ["error"]),
    ],
)
def test_auth_variants(auth: object, path: str, expected: list[str]) -> None:
    assert _levels(_auth_issues(auth), path) == expected


def test_full_rbac_config_is_valid_and_warns_nothing_about_auth() -> None:
    issues = _auth_issues(RBAC)
    assert not [i for i in issues if i["section"] == "auth"]


@pytest.mark.parametrize(
    ("role", "path"),
    [
        ({"permissions": ["x:y"], "reach": "own"}, "auth.roles.bad"),
        ({"permissions": [], "reach": "everyone"}, "auth.roles.bad.reach"),
        ({"permissions": [], "inherits": ["ghost"], "reach": "own"}, "auth.roles.bad"),
        ({"permissions": [], "reach": "tenant"}, "auth.roles.bad"),
    ],
)
def test_invalid_role_is_an_error_beside_the_role(role: dict, path: str) -> None:
    issues = _auth_issues({"method": "jwt", "roles": {"bad": role}})
    assert "error" in _levels(issues, path)
    assert all(i["section"] == "auth" for i in issues if i["path"] == path)


def test_tenant_reach_without_tenant_claims_is_an_error() -> None:
    issues = _auth_issues({"method": "jwt", "roles": {"a": {"permissions": [], "reach": "tenant"}}})
    assert any("tenant_claims" in i["message"] for i in issues if i["level"] == "error")


def test_both_permission_sources_is_an_error() -> None:
    issues = _auth_issues({**RBAC, "permissions_claim": "perms"})
    assert [i for i in issues if i["level"] == "error" and i["section"] == "auth"]


def test_unknown_system_identity_role_is_an_error_at_its_field() -> None:
    auth = {**RBAC, "system_identities": {"job": {"roles": ["ghost"]}}}
    assert _levels(_auth_issues(auth), "auth.system_identities.job.roles") == ["error"]


def test_old_authorization_key_is_an_error_on_the_auth_tab() -> None:
    issues = validate_config({"agent": "graph.react:app", "auth": "jwt", "authorization": "x"})
    found = [i for i in issues if i["path"] == "authorization"]
    assert [(i["level"], i["section"]) for i in found] == [("error", "auth")]
    assert not [i for i in issues if "not read by" in i["message"]]


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


def test_observability_phoenix_block() -> None:
    ok = validate_config(
        {"agent": "graph.react:app", "observability": {"phoenix": {"enabled": True}}}
    )
    assert _levels(ok, "observability") == []
    bad = validate_config(
        {"agent": "graph.react:app", "observability": {"phoenix": {"enabled": "yes"}}}
    )
    assert _levels(bad, "observability.phoenix.enabled") == ["error"]
    assert "observability.phoenix.project" in _fields()


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


def test_wiring_keys_and_checkpointer_warning() -> None:
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


def test_server_refuses_to_save_an_invalid_role(editor: ConfigEditorServer) -> None:
    _, state = _request(editor, "GET", "/api/state")
    bad = {"method": "jwt", "roles": {"x": {"permissions": ["nope:read"], "reach": "own"}}}
    status, body = _request(
        editor,
        "POST",
        "/api/save",
        {"config": {**state["config"], "auth": bad}, "version": state["version"]},
    )
    assert status == 422
    assert any(i["path"] == "auth.roles.x" for i in body["issues"])


def test_server_round_trips_a_full_rbac_config(editor: ConfigEditorServer) -> None:
    _, state = _request(editor, "GET", "/api/state")
    config = {**state["config"], "auth": RBAC}
    status, body = _request(
        editor, "POST", "/api/save", {"config": config, "version": state["version"]}
    )
    assert status == 200
    assert json.loads(editor.store.path.read_text())["auth"] == RBAC
    _, reloaded = _request(editor, "GET", "/api/state")
    assert reloaded["config"]["auth"] == RBAC
    assert not [i for i in reloaded["issues"] if i["level"] == "error"]


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


def test_worker_section_fields_and_key() -> None:
    section = _section("worker")
    assert section["key"] == "worker"
    assert section["optional"] is True
    paths = [tuple(f["path"]) for f in section["fields"]]
    assert paths == [
        ("worker", "broker", "type"),
        ("worker", "broker", "url"),
        ("worker", "result_backend", "type"),
        ("worker", "result_backend", "url"),
        ("worker", "tasks"),
        ("worker", "result_ttl"),
        ("worker", "retries"),
    ]
    assert section_for_key("worker") == "worker"
    issues = validate_config({"agent": "a:b", "worker": section["default"]})
    assert [i for i in issues if i["section"] == "worker"] == []


def test_worker_valid_config_has_no_worker_issues() -> None:
    worker = {
        "broker": {"type": "redis_stream", "url": "${REDIS_URL}"},
        "result_backend": {"type": "redis"},
        "tasks": ["graph.tasks"],
        "result_ttl": 60,
        "retries": 0,
    }
    issues = validate_config({"agent": "a:b", "worker": worker})
    assert [i for i in issues if i["section"] == "worker"] == []


def test_worker_errors_name_the_field() -> None:
    worker = {"broker": {"type": "nope"}, "retries": -1, "result_ttl": 0, "bogus": 1}
    found = validate_config({"agent": "a:b", "worker": worker})
    issues = [i for i in found if i["level"] == "error"]
    paths = {i["path"] for i in issues}
    assert {"worker.broker", "worker.retries", "worker.result_ttl", "worker.bogus"} <= paths
    assert all(i["section"] == "worker" for i in issues)
    assert not any("errors.pydantic.dev" in i["message"] for i in issues)


def test_worker_type_fields_are_selects() -> None:
    fields = {tuple(f["path"]): f for f in _section("worker")["fields"]}
    broker = fields[("worker", "broker", "type")]
    assert broker["type"] == "select"
    assert [o[0] for o in broker["options"]] == ["redis_stream", "rabbitmq", "kafka", "memory"]
    assert broker["options"][0] == ["redis_stream", "Redis Streams"]
    assert broker["default"] == "redis_stream"
    backend = fields[("worker", "result_backend", "type")]
    assert backend["type"] == "select"
    assert [o[0] for o in backend["options"]] == ["redis", "memory"]


@pytest.mark.parametrize(
    ("worker", "path", "message"),
    [
        (
            {"broker": {"type": "my.mod:broker"}},
            "worker.broker.type",
            "'my.mod:broker' is not supported. Supported: redis_stream, rabbitmq, kafka, memory.",
        ),
        (
            {"broker": {"type": "memory"}, "result_backend": {"type": "pkg:backend"}},
            "worker.result_backend.type",
            "'pkg:backend' is not supported. Supported: redis, memory.",
        ),
    ],
)
def test_worker_unsupported_type_is_refused(worker: dict, path: str, message: str) -> None:
    issues = validate_config({"agent": "a:b", "worker": worker})
    matching = [i for i in issues if i["path"] == path]
    assert [i["message"] for i in matching] == [message]


# ---------------------------------------------------------------------------
# AI tracing destinations and metrics
# ---------------------------------------------------------------------------

_FULL_TELEMETRY = {
    "agent": "graph.react:app",
    "observability": {
        "level": "full",
        "logfire": {"enabled": True, "service_name": "svc", "send_to_logfire": False},
        "langsmith": {"enabled": True, "project": "p", "endpoint": "https://smith.example/otel"},
        "phoenix": {"enabled": True, "project": "p", "endpoint": "http://localhost:6006"},
        "otlp": {"enabled": True, "use_metrics_endpoint": True},
    },
    "metrics": {
        "enabled": True,
        "export": "otlp",
        "otlp": {"endpoint": "http://collector:4318"},
        "latency_buckets_ms": [100, 500, 1000],
    },
}


def _section(section_id: str) -> dict:
    return next(s for s in build_schema()["sections"] if s["id"] == section_id)


def test_tracing_and_metrics_sections() -> None:
    tracing, metrics = _section("observability"), _section("metrics")
    assert tracing["widget"] == "observability" and metrics["widget"] == "metrics"
    assert tracing["title"] == "AI tracing" and tracing["key"] == "observability"
    assert tracing["self_managed"] is True and metrics["self_managed"] is True
    assert tracing["notes"] == [] and tracing["default"]["user_id_mode"] == "raw"
    assert all(
        s.get("self_managed", False) is False
        for s in build_schema()["sections"]
        if s["id"] not in ("observability", "metrics")
    )
    assert metrics["key"] == "metrics" and metrics["optional"] is True
    assert "metrics" in KNOWN_TOP_LEVEL_KEYS
    assert section_for_key("metrics") == "metrics"
    paths = {".".join(f["path"]) for f in tracing["fields"]}
    assert {"observability.otlp.endpoint", "observability.otlp.use_metrics_endpoint"} <= paths
    assert all(f["managed"] for f in tracing["fields"] + metrics["fields"])
    assert {".".join(f["path"]) for f in metrics["fields"]} == {
        "metrics.export",
        "metrics.otlp.endpoint",
        "metrics.prometheus.path",
        "metrics.prometheus.worker_port",
        "metrics.latency_buckets_ms",
    }
    names = {e["name"] for e in metrics["env"]}
    assert names == {
        "OTEL_EXPORTER_OTLP_HEADERS",
        "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
        "OTEL_SERVICE_NAME",
    }
    from tenxgraph_api.src.app.core.config.metrics import DEFAULT_BUCKETS_MS

    buckets = next(f for f in metrics["fields"] if f["path"][-1] == "latency_buckets_ms")
    assert buckets["default"] == DEFAULT_BUCKETS_MS
    assert metrics["default"] == {"enabled": True, "export": "otlp", "otlp": {"endpoint": None}}


def test_full_telemetry_config_round_trips(tmp_path: Path) -> None:
    store = ConfigFileStore(tmp_path / "10xgraph.json")
    store.save(_FULL_TELEMETRY, None)
    assert json.loads((tmp_path / "10xgraph.json").read_text()) == _FULL_TELEMETRY
    issues = validate_config(_FULL_TELEMETRY)
    assert [i for i in issues if i["section"] in ("observability", "metrics")] == []


def test_metrics_validation() -> None:
    def check(metrics: object) -> list[dict]:
        issues = validate_config({"agent": "a:b", "metrics": metrics})
        return [i for i in issues if i["section"] == "metrics"]

    assert [i["path"] for i in check({"enabled": True})] == ["metrics.export"]
    assert [i["path"] for i in check({"enabled": True, "export": "otlp"})] == [
        "metrics.otlp.endpoint"
    ]
    assert check({"enabled": True, "export": "prometheus"}) == []
    assert check({"enabled": False}) == []
    assert check({"latency_buckets_ms": [5, 1]})[0]["path"] == "metrics.latency_buckets_ms"
    assert check({"prometheus": {"worker_port": 0}})[0]["path"] == "metrics.prometheus.worker_port"
    assert check("on")[0]["path"] == "metrics"


def test_otlp_tracing_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)

    def check(otlp: dict, metrics: dict | None = None) -> list[dict]:
        config: dict = {"agent": "a:b", "observability": {"otlp": {"enabled": True, **otlp}}}
        if metrics is not None:
            config["metrics"] = metrics
        return [i for i in validate_config(config) if i["path"].startswith("observability")]

    missing = check({})
    assert [(i["level"], i["path"]) for i in missing] == [("error", "observability.otlp.endpoint")]
    assert check({"endpoint": "http://collector:4318"}) == []

    shared = "observability.otlp.use_metrics_endpoint"
    unset = check({"use_metrics_endpoint": True})
    assert [(i["level"], i["path"]) for i in unset] == [("warning", shared)]
    prom_metrics = {"enabled": True, "export": "prometheus"}
    assert [i["level"] for i in check({"use_metrics_endpoint": True}, prom_metrics)] == ["warning"]
    good = {"enabled": True, "export": "otlp", "otlp": {"endpoint": "http://collector:4318"}}
    assert check({"use_metrics_endpoint": True}, good) == []

    prom = {"enabled": True, "export": "otlp", "otlp": {"endpoint": "http://p:9090/api/v1/otlp"}}
    assert [i["path"] for i in check({"use_metrics_endpoint": True}, prom)] == [shared]
    own = check({"endpoint": "http://p:9090/api/v1/otlp"})
    assert [(i["level"], i["path"]) for i in own] == [("warning", "observability.otlp.endpoint")]

    bad = check({"use_metrics_endpoint": "yes"})
    assert _levels(bad, shared) == ["error"]


def test_nothing_traced_warning_counts_otlp() -> None:
    none = validate_config({"agent": "a:b", "observability": {"otlp": {"enabled": False}}})
    warn = next(i for i in none if i["path"] == "observability")
    assert "OTLP" in warn["message"]
    on = validate_config(
        {"agent": "a:b", "observability": {"otlp": {"enabled": True, "endpoint": "http://c:4318"}}}
    )
    assert _levels(on, "observability") == []
