"""``cache_store`` in 10xgraph.json: one object for the store and everything it backs."""

# ruff: noqa: S101

import json
import logging
import re

import pytest

from tenxgraph_api.src.app.core.config.graph_config import (
    CACHE_STORE_KEYS,
    CacheStoreConfig,
    GraphConfig,
    RateLimitConfig,
    TokenRevocationConfig,
)


def _graph_config(tmp_path, data: dict) -> GraphConfig:
    path = tmp_path / "10xgraph.json"
    path.write_text(json.dumps({"agent": "graph.agent:app", **data}))
    return GraphConfig(str(path))


# Messages say where a setting goes and what is valid, never what changed.
_HISTORY = re.compile(r"removed|no longer|deprecated|replaced|\bwas\b|\b\d+\.\d+", re.IGNORECASE)


def _error(value: object) -> str:
    with pytest.raises(ValueError) as info:
        CacheStoreConfig.parse(value)
    message = str(info.value)
    assert not _HISTORY.search(message), message
    return message


def test_absent_or_without_type_is_the_memory_store_with_nothing_on():
    for value in (None, {}):
        config = CacheStoreConfig.parse(value)
        assert config == CacheStoreConfig()
        assert config.type == "memory" and not config.type_set
        assert config.rate_limit is None and config.token_revocation is None


def test_each_type():
    memory = CacheStoreConfig.parse({"type": "memory"})
    assert memory.type == "memory" and memory.type_set

    redis = CacheStoreConfig.parse({"type": "redis", "url": "${REDIS_URL}"})
    assert (redis.type, redis.url, redis.path) == ("redis", "${REDIS_URL}", None)

    custom = CacheStoreConfig.parse({"type": "custom", "path": "my_app.cache:store"})
    assert (custom.type, custom.url, custom.path) == ("custom", None, "my_app.cache:store")


def test_rate_limit_and_token_revocation_live_inside():
    config = CacheStoreConfig.parse(
        {
            "type": "redis",
            "url": "redis://cache:6379/0",
            "rate_limit": {"requests": 5, "window": 10, "by": "user", "fail_open": False},
            "token_revocation": {"token_lifetime": 3600},
        }
    )
    assert config.rate_limit == RateLimitConfig.from_dict(
        {"requests": 5, "window": 10, "by": "user", "fail_open": False}
    )
    assert config.token_revocation == TokenRevocationConfig(token_lifetime=3600)
    # A memory store (type absent) takes them too.
    assert CacheStoreConfig.parse({"token_revocation": True}).token_revocation is not None


def test_graph_config_reads_rate_limit_and_token_revocation_from_cache_store(tmp_path):
    config = _graph_config(
        tmp_path,
        {"cache_store": {"rate_limit": {"requests": 7}, "token_revocation": True}},
    )
    assert config.rate_limit is not None and config.rate_limit.requests == 7
    assert config.token_revocation == TokenRevocationConfig()
    off = _graph_config(tmp_path, {"cache_store": {"rate_limit": {"enabled": False}}})
    assert off.rate_limit is None
    assert off.cache_store.rate_limit is not None  # kept, only reported as off
    empty = _graph_config(tmp_path, {})
    assert empty.rate_limit is None and empty.token_revocation is None
    assert empty.cache_store == CacheStoreConfig()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"type": "redis", "url": ""}, "cache_store.url must be a Redis URL string"),
        ({"type": "redis", "url": 6379}, "cache_store.url must be a Redis URL string"),
        ({"type": "custom"}, 'cache_store.path is required with "type": "custom"'),
        ({"type": "custom", "path": "no_colon"}, 'cache_store.path is required with "type"'),
        ({"url": "redis://x"}, 'cache_store.url goes with "type": "redis"'),
        ({"type": "memory", "url": "redis://x"}, 'cache_store.url goes with "type": "redis"'),
        ({"type": "custom", "path": "a:b", "url": "r://x"}, "cache_store.url goes with"),
        ({"type": "redis", "url": "r://x", "path": "a:b"}, "cache_store.path goes with"),
        ({"type": "valkey"}, "cache_store.type must be one of memory, redis, custom"),
        ("my_app.cache:store", 'cache_store must be an object: {"type": "memory"}'),
        ("redis", "cache_store must be an object"),
    ],
)
def test_invalid_cache_store(value, expected):
    assert expected in _error(value)


def test_unknown_keys_list_the_valid_ones():
    message = _error({"type": "memory", "redis": "redis://x", "limits": {}})
    assert "cache_store: unknown keys ['limits', 'redis']" in message
    assert f"valid keys: {', '.join(CACHE_STORE_KEYS)}" in message

    message = _error({"rate_limit": {"requests": 5, "backend": "redis"}})
    assert "rate_limit: unknown keys ['backend']" in message
    assert "valid keys: enabled, requests, window, by, fail_open" in message

    message = _error({"token_revocation": {"enabled": True, "ttl": 5}})
    assert "token_revocation: unknown keys ['ttl']" in message


def test_string_token_revocation_says_what_is_valid():
    message = _error({"token_revocation": "redis"})
    assert message.startswith(
        "token_revocation must be true, false, or an object with enabled, token_lifetime, "
        "user_ttl"
    )


def test_url_expands_env_vars_and_an_unset_one_is_a_config_error(monkeypatch):
    config = CacheStoreConfig.parse({"type": "redis", "url": "${TG_CS_URL}"})
    monkeypatch.setenv("TG_CS_URL", "redis://cache:6379/0")
    assert config.expanded_url() == "redis://cache:6379/0"
    assert CacheStoreConfig.parse({"type": "redis", "url": "$TG_CS_URL"}).expanded_url() == (
        "redis://cache:6379/0"
    )
    monkeypatch.delenv("TG_CS_URL")
    with pytest.raises(ValueError) as info:
        config.expanded_url()
    assert str(info.value) == (
        "cache_store.url: ${TG_CS_URL} uses an environment variable that is not set"
    )
    assert CacheStoreConfig().expanded_url() is None


def test_redis_without_url_uses_the_redis_url_variable(monkeypatch):
    config = CacheStoreConfig.parse({"type": "redis"})
    monkeypatch.setenv("REDIS_URL", "redis://env-cache:6379/1")
    assert config.expanded_url() == "redis://env-cache:6379/1"
    assert config.url_source == "REDIS_URL"
    own = CacheStoreConfig.parse({"type": "redis", "url": "redis://own:6379/2"})
    assert own.expanded_url() == "redis://own:6379/2"  # a set url wins over REDIS_URL
    assert own.url_source == "cache_store.url"
    monkeypatch.delenv("REDIS_URL")
    with pytest.raises(ValueError, match="set cache_store.url in 10xgraph.json, or the REDIS_URL"):
        config.expanded_url()


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        (
            "rate_limit",
            {"requests": 100},
            '`rate_limit` goes inside `cache_store`: {"cache_store": {"type": ..., '
            '"rate_limit": {...}}}',
        ),
        (
            "token_revocation",
            True,
            '`token_revocation` goes inside `cache_store`: {"cache_store": {"type": ..., '
            '"token_revocation": true}}',
        ),
        (
            "redis",
            "${REDIS_URL}",
            '`redis` goes inside `cache_store` as `url`: {"cache_store": {"type": "redis", '
            '"url": "${REDIS_URL}"}}',
        ),
    ],
)
def test_top_level_store_settings_are_refused_at_load(tmp_path, key, value, expected):
    """A leftover top-level key must not be ignored: rate limiting would silently go off."""
    with pytest.raises(ValueError) as info:
        _graph_config(tmp_path, {key: value})
    assert str(info.value) == expected
    assert not _HISTORY.search(str(info.value))

    # A null is still a key in the wrong place.
    with pytest.raises(ValueError, match=f"`{key}` goes inside `cache_store`"):
        _graph_config(tmp_path, {key: None})


def test_a_top_level_key_added_after_load_is_refused_too(tmp_path):
    config = _graph_config(tmp_path, {"cache_store": {"rate_limit": {}}})
    assert config.rate_limit is not None
    config.data["rate_limit"] = {"requests": 1}
    with pytest.raises(ValueError, match="`rate_limit` goes inside `cache_store`"):
        _ = config.rate_limit


def test_parsed_once_per_raw_value(tmp_path, caplog, monkeypatch):
    """The trusted_proxy_headers warning is not repeated by every caller."""
    monkeypatch.setattr(logging.getLogger("tenxgraph_api"), "propagate", True)
    config = _graph_config(
        tmp_path, {"cache_store": {"rate_limit": {"trusted_proxy_headers": True}}}
    )
    with caplog.at_level(logging.WARNING, logger="tenxgraph_api"):
        for _ in range(3):
            assert config.rate_limit is not None
    warned = {id(r) for r in caplog.records if "trusted_proxy_headers" in r.getMessage()}
    assert len(warned) == 1
    assert config.cache_store is config.cache_store
    config.data["cache_store"] = {"rate_limit": {"requests": 3}}
    assert config.rate_limit is not None and config.rate_limit.requests == 3
