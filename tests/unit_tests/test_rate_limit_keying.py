"""Rate-limit bucket derivation (audit H5).

The headline bug: with `trusted_proxy_headers: true`, the key came from
`X-Forwarded-For.split(",")[0]` -- the LEFTMOST entry, which the caller controls
outright. Sending a different value on every request minted a fresh bucket each
time, so the rate limit could be bypassed entirely.
"""

from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers

from tenxgraph_api.src.app.core.config.graph_config import RateLimitConfig
from tenxgraph_api.src.app.core.middleware.rate_limit import keying
from tenxgraph_api.src.app.core.middleware.rate_limit.keying import client_key_for


def _cfg(**overrides) -> RateLimitConfig:
    base = {
        "enabled": True,
        "requests": 100,
        "window": 60,
        "by": "ip",
        "backend": "redis",
        "redis_url": None,
        "redis_prefix": "p",
        "exclude_paths": (),
        "trusted_proxy_headers": True,
        "trusted_proxy_hops": 1,
        "fail_open": False,
    }
    base.update(overrides)
    return RateLimitConfig(**base)


def _conn(xff: str | None = None, peer: str = "10.0.0.9"):
    return SimpleNamespace(
        headers=Headers({"X-Forwarded-For": xff} if xff else {}),
        client=SimpleNamespace(host=peer),
    )


@pytest.fixture
def verified_as(monkeypatch):
    """Make the limiter see the caller as a verified user (``None`` = no valid token)."""

    def _set(user_id):
        monkeypatch.setattr(keying, "verified_user_id", lambda connection: user_id)

    return _set


class TestForwardedForSpoofing:
    def test_forged_leading_entries_cannot_mint_new_buckets(self):
        """The bypass: forge a different XFF per request, never get limited."""
        cfg = _cfg()
        # Our proxy appends the true peer on the right; everything left is forged.
        keys = {
            client_key_for(_conn(f"{forged}, 203.0.113.7"), cfg)
            for forged in ("1.1.1.1", "2.2.2.2", "3.3.3.3")
        }
        # All must collapse to the SAME bucket -- the real peer.
        assert keys == {"203.0.113.7"}

    def test_uses_nth_from_right_for_multiple_trusted_hops(self):
        # LB -> nginx -> app: the client is 2 back from the right.
        cfg = _cfg(trusted_proxy_hops=2)
        key = client_key_for(_conn("1.1.1.1, 203.0.113.7, 10.0.0.2"), cfg)
        assert key == "203.0.113.7"

    def test_short_header_is_not_trusted(self):
        # Fewer entries than our infrastructure should have appended -> ignore it
        # and fall back to the real peer address.
        cfg = _cfg(trusted_proxy_hops=2)
        assert client_key_for(_conn("1.1.1.1"), cfg) == "10.0.0.9"

    def test_header_ignored_entirely_when_not_trusted(self):
        cfg = _cfg(trusted_proxy_headers=False)
        assert client_key_for(_conn("1.1.1.1, 203.0.113.7"), cfg) == "10.0.0.9"


class TestByUser:
    def test_authenticated_user_gets_own_bucket(self, verified_as):
        verified_as("u42")
        key = client_key_for(_conn(), _cfg(by="user"))
        assert key == "user:u42"

    def test_two_users_do_not_share_a_bucket(self, verified_as):
        cfg = _cfg(by="user")
        verified_as("a")
        a = client_key_for(_conn(), cfg)
        verified_as("b")
        b = client_key_for(_conn(), cfg)
        assert a != b

    def test_anonymous_falls_back_to_ip_not_one_shared_bucket(self, verified_as):
        # Otherwise a single anonymous caller could exhaust the bucket for everyone.
        verified_as(None)
        key = client_key_for(_conn("1.1.1.1, 203.0.113.7"), _cfg(by="user"))
        assert key == "ip:203.0.113.7"


class TestConfigValidation:
    def test_by_user_is_accepted(self):
        cfg = RateLimitConfig.from_dict({"by": "user", "backend": "redis"})
        assert cfg.by == "user"

    def test_invalid_by_rejected(self):
        with pytest.raises(ValueError, match="rate_limit.by"):
            RateLimitConfig.from_dict({"by": "nonsense"})

    def test_zero_proxy_hops_rejected(self):
        with pytest.raises(ValueError, match="trusted_proxy_hops"):
            RateLimitConfig.from_dict({"trusted_proxy_hops": 0})

    def test_global_still_supported(self):
        cfg = _cfg(by="global")
        assert client_key_for(_conn(), cfg) == "__global__"


class TestDefaultTrustedProxies:
    """An empty trusted_proxies list trusts only loopback and private peers."""

    @pytest.mark.parametrize("peer", ["10.1.2.3", "172.20.0.4", "192.168.1.1", "127.0.0.1", "::1"])
    def test_private_peer_is_trusted(self, peer):
        assert client_key_for(_conn("1.1.1.1, 203.0.113.7", peer=peer), _cfg()) == "203.0.113.7"

    @pytest.mark.parametrize("peer", ["198.51.100.7", "2001:db8::1", "not-an-ip"])
    def test_public_or_unknown_peer_is_keyed_by_itself(self, peer):
        assert client_key_for(_conn("1.1.1.1, 203.0.113.7", peer=peer), _cfg()) == peer

    def test_explicit_catch_all_restores_trust_everyone(self):
        cfg = _cfg(trusted_proxies=("0.0.0.0/0", "::/0"))
        assert client_key_for(_conn("203.0.113.7", peer="198.51.100.7"), cfg) == "203.0.113.7"

    def test_explicit_list_replaces_the_default(self):
        cfg = _cfg(trusted_proxies=("198.51.100.0/24",))
        assert client_key_for(_conn("203.0.113.7", peer="198.51.100.7"), cfg) == "203.0.113.7"
        assert client_key_for(_conn("203.0.113.7", peer="10.0.0.9"), cfg) == "10.0.0.9"

    def test_parsing_warns_when_headers_are_trusted_without_a_list(self, caplog):
        with caplog.at_level("WARNING", logger="tenxgraph_api"):
            RateLimitConfig.from_dict({"backend": "redis", "trusted_proxy_headers": True})
        assert "without rate_limit.trusted_proxies" in caplog.text
        caplog.clear()
        with caplog.at_level("WARNING", logger="tenxgraph_api"):
            RateLimitConfig.from_dict(
                {
                    "backend": "redis",
                    "trusted_proxy_headers": True,
                    "trusted_proxies": ["10.0.0.0/8"],
                }
            )
        assert "without rate_limit.trusted_proxies" not in caplog.text
