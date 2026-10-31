"""``by: "user"`` must limit per verified user, and client IPs must not be spoofable."""

# ruff: noqa: S101, PLR2004

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
from tenxgraph_api.src.app.core.cache_store import InMemoryCacheStore
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig, RateLimitConfig
from tenxgraph_api.src.app.core.middleware.rate_limit import RateLimitMiddleware
from tenxgraph_api.src.app.core.middleware.rate_limit.keying import client_key_for


class TokenIsUserAuth(BaseAuth):
    """Accepts ``Bearer valid-<name>`` as user ``<name>``; anything else is a 401."""

    def authenticate(self, request, response, credential):
        token = credential.credentials if credential else ""
        if not token.startswith("valid-"):
            raise HTTPException(status_code=401, detail="bad token")
        return {"user_id": token.removeprefix("valid-")}


def _config(**overrides) -> RateLimitConfig:
    data = {"enabled": True, "requests": 2, "window": 60, "by": "user"}
    data.update(overrides)
    return RateLimitConfig.from_dict(data)


@pytest.fixture
def auth_container():
    graph_config = MagicMock(spec=GraphConfig)
    graph_config.auth_config.return_value = {"method": "custom", "path": "x:y"}
    bindings = {GraphConfig: graph_config, BaseAuth: TokenIsUserAuth()}
    container = MagicMock()
    container.try_get.side_effect = lambda key, default=None: bindings.get(key, default)
    with patch("injectq.InjectQ.get_instance", return_value=container):
        yield


def _app(config: RateLimitConfig) -> TestClient:
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, config=config, cache=InMemoryCacheStore())

    @app.get("/")
    def root():
        return {"ok": True}

    return TestClient(app)


def _get(client: TestClient, token: str | None = None, **headers):
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.get("/", headers=headers).status_code


def test_each_verified_user_gets_their_own_budget(auth_container):
    client = _app(_config())
    # Same IP (the test client), two users: each gets the full limit of 2.
    assert [_get(client, "valid-alice") for _ in range(2)] == [200, 200]
    assert [_get(client, "valid-bob") for _ in range(2)] == [200, 200]
    assert _get(client, "valid-alice") == 429


def test_invalid_tokens_fall_back_to_the_ip_bucket(auth_container):
    client = _app(_config())
    # A different bogus token each time must not buy a fresh bucket.
    assert [_get(client, f"forged-{i}") for i in range(3)] == [200, 200, 429]


def test_anonymous_requests_use_the_ip_bucket(auth_container):
    client = _app(_config())
    assert [_get(client) for _ in range(3)] == [200, 200, 429]


def test_user_mode_without_auth_configured_uses_ip():
    container = MagicMock()
    container.try_get.return_value = None
    with patch("injectq.InjectQ.get_instance", return_value=container):
        client = _app(_config())
        assert [_get(client, "valid-alice") for _ in range(3)] == [200, 200, 429]


# ---------------------------------------------------------------------------
# X-Forwarded-For
# ---------------------------------------------------------------------------


def _request(headers: list[tuple[str, str]], peer: str = "10.0.0.5") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
            "client": (peer, 1234),
        }
    )


def test_repeated_forwarded_for_headers_are_combined():
    # A proxy that appends its own header line (instead of merging) must not let the
    # attacker's first line win.
    config = _config(by="ip", trusted_proxy_headers=True)
    request = _request([("X-Forwarded-For", "6.6.6.6"), ("X-Forwarded-For", "203.0.113.9")])
    assert client_key_for(request, config) == "203.0.113.9"


def test_forwarded_for_ignored_from_untrusted_peer():
    config = _config(by="ip", trusted_proxy_headers=True, trusted_proxies=["10.0.0.0/8"])
    direct = _request([("X-Forwarded-For", "6.6.6.6")], peer="198.51.100.7")
    assert client_key_for(direct, config) == "198.51.100.7"


def test_forwarded_for_honoured_from_trusted_proxy():
    config = _config(by="ip", trusted_proxy_headers=True, trusted_proxies=["10.0.0.0/8"])
    via_proxy = _request([("X-Forwarded-For", "203.0.113.9")], peer="10.0.0.5")
    assert client_key_for(via_proxy, config) == "203.0.113.9"


def test_trusted_proxies_must_be_valid_networks():
    with pytest.raises(ValueError, match="trusted_proxies"):
        _config(trusted_proxies=["not-an-ip"])
