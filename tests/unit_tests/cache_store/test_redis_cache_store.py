"""RedisCacheStore without a server: key layout, commands sent, replies, ownership.

The Lua scripts themselves run only against a real Redis (the contract suite with
``TEST_REDIS_URL``).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tenxgraph_api.src.app.core.auth import revocation
from tenxgraph_api.src.app.core.auth.ownership_resolver import ThreadOwnershipResolver
from tenxgraph_api.src.app.core.cache_store import redis_store
from tenxgraph_api.src.app.core.cache_store.redis_store import RedisCacheStore
from tenxgraph_api.src.app.core.config import settings as settings_module
from tenxgraph_api.src.app.core.redis_client import key_part, prefixed


class NoScriptError(Exception):
    pass


def _client():
    client = MagicMock()
    for name in ("ping", "time", "evalsha", "eval", "zrem", "get", "set", "delete", "aclose"):
        setattr(client, name, AsyncMock())
    return client


def _store(client=None, prefix=None):
    return RedisCacheStore(client=client or _client(), prefix=prefix)


@pytest.fixture
def deploy_prefix(monkeypatch):
    real = settings_module.get_settings()
    fake = SimpleNamespace(**{**real.model_dump(), "REDIS_KEY_PREFIX": "dep"})
    monkeypatch.setattr(settings_module, "get_settings", lambda: fake)
    return "dep"


# Key layout


@pytest.mark.parametrize("prefix", [None, "dep"])
def test_keys_match_the_layout_used_before_the_cache_store(request, prefix):
    """Old and new pods must share locks, records and revocations during a rolling deploy."""
    if prefix:
        request.getfixturevalue("deploy_prefix")
    store = RedisCacheStore(client=_client())

    # RedisConcurrencyLimiter: f"{prefixed('10xgraph:concurrency')}:{lease key}"
    for lease in ("runs:thread:42", "runs:user:u%3A1", "runs:idem:u:k", "ws:all", "ws:user:u"):
        assert (
            store.redis_key(f"concurrency:{lease}") == f"{prefixed('10xgraph:concurrency')}:{lease}"
        )

    # The ownership resolver used f"{prefixed('af:authz:owner')}:id:<thread>" and ":tomb:".
    thread = "th:1"
    owner_ns = prefixed("af:authz:owner")
    owner_key = ThreadOwnershipResolver._owner_key(thread)
    assert store.redis_key(owner_key) == f"{owner_ns}:id:{key_part(thread)}"
    tomb_key = ThreadOwnershipResolver._tombstone_key(thread)
    assert store.redis_key(tomb_key) == f"{owner_ns}:tomb:{key_part(thread)}"

    # Token revocation used f"{prefixed('10xgraph:revoked')}:jti:<jti>" and ":user:".
    revoked_ns = prefixed("10xgraph:revoked")
    assert store.redis_key(revocation.jti_key("j:1")) == f"{revoked_ns}:jti:{key_part('j:1')}"
    assert store.redis_key(revocation.user_key("u:1")) == f"{revoked_ns}:user:{key_part('u:1')}"

    # routers/graph/idempotency.py: f"{prefixed('10xgraph:idempotency')}:{ident}"
    ident = f"{key_part('u1')}:{key_part('key:1')}"
    assert store.redis_key(f"idempotency:{ident}") == f"{prefixed('10xgraph:idempotency')}:{ident}"

    lead = f"{prefix}:" if prefix else ""
    assert store.redis_key("ratelimit:ip:1.2.3.4") == f"{lead}10xgraph:ratelimit2:ip:1.2.3.4"
    assert store.redis_key("other:x") == f"{lead}10xgraph:cache:other:x"


def test_explicit_prefix_overrides_the_setting(deploy_prefix):
    assert _store(prefix="").redis_key("concurrency:a") == "10xgraph:concurrency:a"
    assert _store(prefix="p").redis_key("owner:id:a") == "p:af:authz:owner:id:a"


def test_unknown_and_bare_keys_cannot_collide_with_feature_keys():
    store = _store(prefix="")
    assert store.redis_key("plain") == "10xgraph:cache:plain"
    assert store.redis_key("concurrency") == "10xgraph:cache:concurrency"
    assert store.redis_key("cache:concurrency:a") == "10xgraph:cache:cache:concurrency:a"
    assert store.redis_key("concurrency:a") != store.redis_key("cache:concurrency:a")


# Construction, describe, aclose


def test_needs_exactly_one_of_url_or_client():
    with pytest.raises(ValueError):
        RedisCacheStore()
    with pytest.raises(ValueError):
        RedisCacheStore("redis://localhost", client=_client())


def test_describe_never_shows_credentials():
    store = RedisCacheStore.from_url("redis://user:s3cret@cache.internal:6380/2")
    assert store.describe() == "cache.internal:6380"
    assert "s3cret" not in (store.describe() or "")
    assert RedisCacheStore("rediss://:pw@[::1]:6379/0").describe() == "[::1]:6379"
    assert RedisCacheStore("unix:///tmp/redis.sock").describe() is None


def test_describe_for_a_passed_client_uses_its_pool():
    client = _client()
    client.connection_pool.connection_kwargs = {"host": "h", "port": 7000, "password": "pw"}
    assert _store(client).describe() == "h:7000"
    client.connection_pool.connection_kwargs = {}
    assert _store(client).describe() is None


@pytest.mark.asyncio
async def test_aclose_closes_only_a_client_it_created(monkeypatch):
    created = _client()
    monkeypatch.setattr(redis_store, "create_redis", lambda url: created)
    await RedisCacheStore("redis://localhost:6379").aclose()
    created.aclose.assert_awaited_once()

    passed = _client()
    await RedisCacheStore(client=passed).aclose()
    passed.aclose.assert_not_awaited()


# Commands


@pytest.mark.asyncio
async def test_now_uses_the_server_clock():
    client = _client()
    client.time.return_value = (1_700_000_000, 250_000)
    assert await _store(client).now() == 1_700_000_000.25


@pytest.mark.asyncio
async def test_aping_sends_ping():
    client = _client()
    await _store(client).aping()
    client.ping.assert_awaited_once()


@pytest.mark.asyncio
async def test_acquire_decodes_a_grant():
    client = _client()
    client.evalsha.return_value = [1, 2, b"1700000000.500000"]
    result = await _store(client, prefix="").acquire("concurrency:runs:thread:1", 3, 60)
    assert result.granted and result.count == 2
    assert result.oldest_expiry == 1_700_000_000.5
    assert result.slot_id is not None
    sha, numkeys, key, limit, ttl, slot_id, margin = client.evalsha.await_args.args
    assert sha == redis_store._ACQUIRE.sha and numkeys == 1
    assert key == "10xgraph:concurrency:runs:thread:1"
    assert (limit, ttl, slot_id, margin) == (3, "60.0", result.slot_id, 1000)


@pytest.mark.asyncio
async def test_acquire_decodes_a_denial_on_an_empty_set():
    client = _client()
    client.evalsha.return_value = [0, 0, b""]
    result = await _store(client).acquire("ratelimit:k", 0, 60)
    assert not result.granted and result.count == 0
    assert result.oldest_expiry is None and result.slot_id is None


@pytest.mark.asyncio
async def test_script_is_loaded_when_the_server_does_not_have_it():
    client = _client()
    client.evalsha.side_effect = NoScriptError("NOSCRIPT No matching script")
    client.eval.return_value = 1
    assert await _store(client).renew("concurrency:k", "slot", 60)
    assert client.eval.await_args.args[0] == redis_store._RENEW_LUA


@pytest.mark.asyncio
async def test_other_script_errors_are_raised():
    client = _client()
    client.evalsha.side_effect = ConnectionError("down")
    with pytest.raises(ConnectionError):
        await _store(client).acquire("concurrency:k", 1, 60)
    client.eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_renew_reports_a_missing_slot():
    client = _client()
    client.evalsha.return_value = 0
    assert not await _store(client).renew("concurrency:k", "gone", 60)


@pytest.mark.asyncio
async def test_release_is_zrem():
    client = _client()
    await _store(client, prefix="").release("concurrency:k", "slot")
    client.zrem.assert_awaited_once_with("10xgraph:concurrency:k", "slot")


@pytest.mark.asyncio
async def test_set_modes_send_the_right_commands():
    client = _client()
    store = _store(client, prefix="")
    assert await store.set("idempotency:a", b"v", 2.5)
    client.set.assert_awaited_with("10xgraph:idempotency:a", b"v", px=2500)
    assert await store.set("idempotency:a", b"v", None)
    client.set.assert_awaited_with("10xgraph:idempotency:a", b"v", px=None)

    client.set.return_value = None
    assert not await store.set("idempotency:a", b"v", 1, "if_absent")
    client.set.assert_awaited_with("10xgraph:idempotency:a", b"v", px=1000, nx=True)
    client.set.return_value = True
    assert await store.set("idempotency:a", b"v", 1, "if_absent")

    client.evalsha.return_value = 1
    assert await store.set("revoked:user:u", b"1700000000.000001", 30, "if_greater")
    args = client.evalsha.await_args.args
    assert args[0] == redis_store._IF_GREATER.sha
    assert args[2:] == ("10xgraph:revoked:user:u", "1700000000.000001", "30000")
    client.evalsha.return_value = 0
    assert not await store.set("revoked:user:u", b"1", None, "if_greater")
    assert client.evalsha.await_args.args[4] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [b"abc", b"", b"0x10", b"1_000", b"inf", b"nan", b"1e999"])
async def test_if_greater_rejects_non_decimal_values(bad):
    client = _client()
    with pytest.raises(ValueError):
        await _store(client).set("revoked:user:u", bad, None, "if_greater")
    client.evalsha.assert_not_awaited()


@pytest.mark.asyncio
async def test_ttl_and_mode_are_validated_before_any_command():
    client = _client()
    store = _store(client)
    with pytest.raises(ValueError):
        await store.acquire("concurrency:k", 1, 0)
    with pytest.raises(ValueError):
        await store.set("k", b"v", -1)
    with pytest.raises(ValueError):
        await store.set("k", b"v", 1, "sometimes")  # type: ignore[arg-type]
    client.evalsha.assert_not_awaited()
    client.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_returns_bytes_from_a_decoding_client():
    client = _client()
    client.get.return_value = "text"
    assert await _store(client).get("idempotency:a") == b"text"
    client.get.return_value = None
    assert await _store(client).get("idempotency:a") is None


@pytest.mark.asyncio
async def test_delete_is_del():
    client = _client()
    await _store(client, prefix="").delete("owner:id:t")
    client.delete.assert_awaited_once_with("af:authz:owner:id:t")


def test_scripts_touch_one_key_and_use_the_server_clock():
    for script in (redis_store._ACQUIRE_LUA, redis_store._RENEW_LUA):
        assert "redis.call('TIME')" in script
        assert "KEYS[2]" not in script
    assert "'XX'" in redis_store._RENEW_LUA
    assert "KEYS[2]" not in redis_store._IF_GREATER_LUA
