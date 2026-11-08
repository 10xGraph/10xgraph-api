"""``runs`` config and the server-picked thread id mode."""

# ruff: noqa: S101

import pytest

from tenxgraph_api.cli.config_editor import validate_config as validate
from tenxgraph_api.src.app.core.config.graph_config import (
    DEFAULT_MAX_CONCURRENT_RUNS_PER_USER,
    GraphConfig,
    RunsConfig,
)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def test_defaults(monkeypatch):
    monkeypatch.delenv("MODE", raising=False)
    runs = RunsConfig.from_dict({})
    assert runs == RunsConfig()
    assert runs.thread_ids == "server"
    assert runs.server_thread_ids is True
    assert runs.config_keys is None
    assert runs.state_keys is None
    assert runs.max_concurrent_per_user == DEFAULT_MAX_CONCURRENT_RUNS_PER_USER
    assert runs.timeout is None
    assert runs.max_parallel_tool_calls is None


def test_timeout_defaults_to_300_seconds_in_production(monkeypatch):
    monkeypatch.setenv("MODE", "production")
    assert RunsConfig().timeout == 300.0
    assert RunsConfig.from_dict({}).timeout == 300.0
    assert RunsConfig.from_dict({"thread_ids": "server"}).timeout == 300.0


@pytest.mark.parametrize("mode", ["development", "dev", "", "bogus"])
def test_timeout_is_unlimited_in_development(monkeypatch, mode):
    monkeypatch.setenv("MODE", mode)
    assert RunsConfig().timeout is None
    assert RunsConfig.from_dict({}).timeout is None


def test_explicit_timeout_wins_in_production(monkeypatch):
    monkeypatch.setenv("MODE", "production")
    assert RunsConfig.from_dict({"timeout": 90}).timeout == 90.0
    assert RunsConfig.from_dict({"timeout": 1200}).timeout == 1200.0


def test_explicit_null_timeout_means_no_limit_in_production(monkeypatch):
    monkeypatch.setenv("MODE", "production")
    assert RunsConfig.from_dict({"timeout": None}).timeout is None


def test_graph_config_without_runs_key_gets_the_production_default(monkeypatch, tmp_path):
    monkeypatch.setenv("MODE", "production")
    path = tmp_path / "10xgraph.json"
    path.write_text('{"agent": "graph.react:app"}')
    assert GraphConfig(str(path)).runs.timeout == 300.0


def test_full_config():
    runs = RunsConfig.from_dict(
        {
            "thread_ids": "server",
            "config_keys": ["locale", " tenant "],
            "state_keys": [],
            "max_concurrent_per_user": 3,
            "timeout": 90,
            "max_parallel_tool_calls": 4,
        }
    )
    assert runs.server_thread_ids is True
    assert runs.config_keys == frozenset({"locale", "tenant"})
    assert runs.state_keys == frozenset()
    assert runs.max_concurrent_per_user == 3
    assert runs.timeout == 90.0
    assert runs.max_parallel_tool_calls == 4


@pytest.mark.parametrize("value", [0, None])
def test_zero_or_null_concurrency_means_unlimited(value):
    assert RunsConfig.from_dict({"max_concurrent_per_user": value}).max_concurrent_per_user is None


@pytest.mark.parametrize(
    ("data", "match"),
    [
        ("server", "runs must be an object"),
        ({"thread_ids": "anyone"}, "thread_ids"),
        ({"config_keys": "locale"}, "config_keys"),
        ({"state_keys": [1]}, "state_keys"),
        ({"max_concurrent_per_user": -1}, "max_concurrent_per_user"),
        ({"max_concurrent_per_user": "5"}, "max_concurrent_per_user"),
        ({"max_concurrent_per_user": True}, "max_concurrent_per_user"),
        ({"timeout": 0}, "timeout"),
        ({"timeout": -5}, "timeout"),
        ({"timeout": "60"}, "timeout"),
        ({"max_parallel_tool_calls": 0}, "max_parallel_tool_calls"),
        ({"max_parallel_tool_calls": -2}, "max_parallel_tool_calls"),
        ({"max_parallel_tool_calls": 2.5}, "max_parallel_tool_calls"),
        ({"max_parallel_tool_calls": "5"}, "max_parallel_tool_calls"),
        ({"max_parallel_tool_calls": True}, "max_parallel_tool_calls"),
    ],
)
def test_invalid_values_are_rejected(data, match):
    with pytest.raises(ValueError, match=match):
        RunsConfig.from_dict(data)


def test_graph_config_property(tmp_path):
    path = tmp_path / "10xgraph.json"
    path.write_text('{"agent": "graph:app", "runs": {"thread_ids": "server"}}')
    assert GraphConfig(str(path)).runs.server_thread_ids is True
    path.write_text('{"agent": "graph:app"}')
    assert GraphConfig(str(path)).runs == RunsConfig()


def test_config_editor_knows_and_validates_runs():
    base = {"agent": "graph:app"}
    assert not [
        i
        for i in validate({**base, "runs": {"thread_ids": "server"}})
        if i["path"].startswith("runs") and i["level"] == "error"
    ]
    issues = validate({**base, "runs": {"thread_ids": "nobody"}})
    assert [i["path"] for i in issues if i["level"] == "error"] == ["runs"]


# ---------------------------------------------------------------------------
# server-picked thread ids
# ---------------------------------------------------------------------------


def test_client_is_an_explicit_opt_in():
    runs = RunsConfig.from_dict({"thread_ids": "client"})
    assert runs.thread_ids == "client"
    assert runs.server_thread_ids is False


def test_config_editor_validates_max_parallel_tool_calls():
    base = {"agent": "graph:app"}
    issues = validate({**base, "runs": {"max_parallel_tool_calls": 5}})
    assert not [i for i in issues if i["path"].startswith("runs")]
    issues = validate({**base, "runs": {"max_parallel_tool_calls": 0}})
    assert [i["path"] for i in issues if i["level"] == "error"] == ["runs"]
