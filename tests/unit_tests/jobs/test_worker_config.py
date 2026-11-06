"""The ``worker`` section: parsing, validation messages, ${VAR} expansion, GraphConfig."""

from __future__ import annotations

import json

import pytest

from tenxgraph_api.jobs.config import WorkerConfigError, expand_env, parse_worker_config
from tenxgraph_api.jobs.service import parse_reserve
from tenxgraph_api.jobs.worker import check_reserve
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig


PROD = {
    "modules": ["jobs.a", "jobs.b"],
    "store": {"type": "postgres", "url": "${DATABASE_URL}", "schema": "jobs"},
    "transport": {"type": "redis", "url": "${REDIS_URL}"},
    "result_ttl": 3600,
    "system_identities": {"nightly": {"user_id": "__system__:nightly", "role": "cron"}},
    "hooks": "audit:Audit",
    "publish_events": False,
    "lease_ttl": 30,
    "shutdown_timeout": 10,
}


def test_absent_is_none():
    assert parse_worker_config(None) is None


def test_local_shortcut():
    cfg = parse_worker_config({"modules": "jobs", "backend": "local"})
    assert cfg.modules == ["jobs"]
    assert cfg.store_type == cfg.transport_type == "sqlite"
    assert cfg.store_path == cfg.transport_path == ".10xgraph/jobs.db"
    assert cfg.redis_db == 1
    assert cfg.result_ttl == 604800
    assert cfg.lease_ttl == 60
    assert cfg.shutdown_timeout == 30
    assert cfg.publish_events is True


def test_production_section():
    cfg = parse_worker_config(PROD)
    assert cfg.store.type == "postgres"
    assert cfg.store.db_schema == "jobs"
    assert cfg.store.auto_migrate is True
    assert cfg.transport.type == "redis"
    assert cfg.hooks == ["audit:Audit"]
    assert cfg.system_identity("nightly") == {"user_id": "__system__:nightly", "role": "cron"}
    assert cfg.system_identity("nope") is None
    # Parsing keeps ${VAR} as written: nothing is read from the environment yet.
    assert cfg.store.url == "${DATABASE_URL}"


def test_unknown_keys_list_the_valid_ones():
    with pytest.raises(WorkerConfigError) as err:
        parse_worker_config({"backend": "local", "concurency": 4})
    message = str(err.value)
    assert "unknown keys ['concurency']" in message
    for key in ("modules", "backend", "store", "transport", "redis_db", "lease_ttl"):
        assert key in message


def test_unknown_nested_keys():
    raw = {"store": {"type": "redis", "url": "redis://x", "db": 2}, "transport": {"type": "redis", "url": "redis://x"}}
    with pytest.raises(WorkerConfigError, match=r"worker\.store: unknown keys \['db'\]; valid keys: type, url, path, schema, auto_migrate"):
        parse_worker_config(raw)
    raw = {"store": {"type": "redis", "url": "redis://x"}, "transport": {"type": "redis", "url": "redis://x", "group": "g"}}
    with pytest.raises(WorkerConfigError, match=r"worker\.transport: unknown keys \['group'\]"):
        parse_worker_config(raw)


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ("local", "worker must be an object"),
        ({"modules": "jobs"}, 'needs "store" and "transport"'),
        ({"backend": "local", "store": {"type": "memory"}}, 'backend "local" sets both'),
        ({"backend": "cloud"}, r"worker\.backend"),
        (
            {"store": {"type": "postgres"}, "transport": {"type": "redis", "url": "r"}},
            r"worker\.store\.url is required",
        ),
        (
            {"store": {"type": "mysql", "url": "x"}, "transport": {"type": "redis", "url": "r"}},
            r"worker\.store\.type: must be one of",
        ),
        (
            {"store": {"type": "postgres", "url": "x"}, "transport": {"type": "sqlite"}},
            'transport "sqlite" needs worker.store "sqlite"',
        ),
        (
            {"store": {"type": "memory"}, "transport": {"type": "redis", "url": "r"}},
            '"memory" go together',
        ),
        (
            {"store": {"type": "sqlite", "url": "x"}, "transport": {"type": "sqlite"}},
            r"worker\.store\.url goes with",
        ),
        (
            {"store": {"type": "redis", "url": "r", "schema": "s"}, "transport": {"type": "redis", "url": "r"}},
            "schema goes with",
        ),
        ({"backend": "local", "modules": ["not a module"]}, "dotted module path"),
        ({"backend": "local", "hooks": ["nocolon"]}, "module:attr"),
        ({"backend": "local", "redis_db": -1}, r"worker\.redis_db"),
        ({"backend": "local", "lease_ttl": 0}, r"worker\.lease_ttl"),
        ({"backend": "local", "system_identities": {"x": {"user_id": " "}}}, "user_id"),
        (
            {"store": {"type": "sqlite", "path": "a.db"}, "transport": {"type": "sqlite", "path": "b.db"}},
            "same file",
        ),
    ],
)
def test_invalid_sections(raw, match):
    with pytest.raises(WorkerConfigError, match=match):
        parse_worker_config(raw)


def test_env_expansion(monkeypatch):
    monkeypatch.setenv("JOBS_DB", "postgresql://u@h/db")
    assert expand_env("${JOBS_DB}", field="worker.store.url") == "postgresql://u@h/db"
    assert expand_env("$JOBS_DB?x=1", field="f") == "postgresql://u@h/db?x=1"
    monkeypatch.delenv("JOBS_DB")
    with pytest.raises(WorkerConfigError, match="worker.store.url uses environment variables that are not set: JOBS_DB"):
        expand_env("${JOBS_DB}", field="worker.store.url")
    cfg = parse_worker_config(PROD)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(WorkerConfigError, match="DATABASE_URL"):
        cfg.store.expanded_url()


def test_graph_config_worker(tmp_path):
    path = tmp_path / "10xgraph.json"
    path.write_text(json.dumps({"agent": "graph:app"}))
    assert GraphConfig(str(path)).worker is None
    path.write_text(json.dumps({"agent": "graph:app", "worker": {"backend": "local", "modules": "jobs"}}))
    gc = GraphConfig(str(path))
    assert gc.worker is gc.worker  # parsed once
    assert gc.worker.modules == ["jobs"]
    path.write_text(json.dumps({"agent": "graph:app", "worker": {"backend": "local", "x": 1}}))
    with pytest.raises(ValueError, match="unknown keys"):
        _ = GraphConfig(str(path)).worker


def test_reserve_parsing_and_checks():
    assert parse_reserve(["high=2", "normal=1"]) == {"high": 2, "normal": 1}
    assert parse_reserve(None) == {}
    for bad in ("high", "urgent=2", "high=x", "high=-1"):
        with pytest.raises(ValueError, match="--reserve"):
            parse_reserve([bad])
    assert check_reserve({"high": 2}, 16) == {"high": 2}
    with pytest.raises(ValueError, match="cannot hold slots|can hold slots"):
        check_reserve({"low": 1}, 16)
    with pytest.raises(ValueError, match="leave none"):
        check_reserve({"high": 2}, 2)
