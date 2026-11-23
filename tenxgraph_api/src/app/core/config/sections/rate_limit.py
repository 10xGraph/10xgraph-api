"""``rate_limit`` section of 10xgraph.json."""

import ipaddress
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    pass
from tenxgraph_api.src.app.core.config.sections.common import _parse_bool


logger = logging.getLogger("tenxgraph_api")


# Where X-Forwarded-For is trusted from when ``trusted_proxy_headers`` is on but no
# ``trusted_proxies`` are listed: loopback and private networks, which is where a reverse proxy,
# ingress or sidecar in front of the app connects from. A client on the public internet that
# reaches the app directly is keyed by its own address. List ``0.0.0.0/0`` and ``::/0`` to
# trust every peer (only when nothing can reach the app except through your proxy).
DEFAULT_TRUSTED_PROXIES: tuple[str, ...] = (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "100.64.0.0/10",
    "::1/128",
    "fc00::/7",
    "fe80::/10",
)


def _parse_networks(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list | tuple):
        raise ValueError("rate_limit.trusted_proxies must be a list of IPs or CIDR ranges")
    networks = []
    for item in raw:
        try:
            networks.append(str(ipaddress.ip_network(str(item).strip(), strict=False)))
        except ValueError as exc:
            raise ValueError(f"rate_limit.trusted_proxies: invalid network {item!r}") from exc
    return tuple(networks)


RATE_LIMIT_KEYS: tuple[str, ...] = (
    "enabled",
    "requests",
    "window",
    "by",
    "fail_open",
    "trusted_proxy_headers",
    "trusted_proxies",
    "trusted_proxy_hops",
    "exclude_paths",
)


@dataclass
class RateLimitConfig:
    """Rate limit configuration: ``cache_store.rate_limit`` in 10xgraph.json.

    Example::

        "cache_store": {
            "type": "redis",
            "url": "${REDIS_URL}",
            "rate_limit": {
                "enabled": true,
                "requests": 100,
                "window": 60,
                "by": "ip",
                "trusted_proxy_headers": false,
                "exclude_paths": ["/ping", "/docs", "/redocs", "/openapi.json"],
                "fail_open": true
            }
        }

    The counters live in the API's cache store, like the run locks and caps: shared between
    workers with a Redis or custom store, per process with the in-memory one.
    """

    enabled: bool
    requests: int
    window: int
    by: str  # "ip" | "user" | "global"
    exclude_paths: tuple[str, ...]
    trusted_proxy_headers: bool  # honour X-Forwarded-For only when True
    # Number of proxies you control in front of the app. Only this many entries,
    # counted from the RIGHT of X-Forwarded-For, were appended by your own
    # infrastructure; anything further left came from the caller and is forgeable.
    trusted_proxy_hops: int = 1
    fail_open: bool = True  # on a cache store error: True=allow, False=deny with 503
    # Networks your proxies connect from. X-Forwarded-For is honoured only for requests
    # whose peer address is in one of them, so a client that reaches the app directly
    # (bypassing the proxy) cannot pick its own address. Empty means
    # DEFAULT_TRUSTED_PROXIES (loopback and private networks), never "everyone".
    trusted_proxies: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict) -> "RateLimitConfig":
        if not isinstance(data, dict):
            raise ValueError("rate_limit must be an object")
        unknown = sorted(set(data) - set(RATE_LIMIT_KEYS))
        if unknown:
            raise ValueError(
                f"rate_limit: unknown keys {unknown}; valid keys: {', '.join(RATE_LIMIT_KEYS)}"
            )

        enabled = _parse_bool(data.get("enabled", True), field="rate_limit.enabled")
        requests = int(data.get("requests", 100))
        window = int(data.get("window", 60))
        by = data.get("by", "ip")
        trusted_proxy_headers = _parse_bool(
            data.get("trusted_proxy_headers", False),
            field="rate_limit.trusted_proxy_headers",
        )
        exclude_paths_raw = data.get("exclude_paths", [])
        if not isinstance(exclude_paths_raw, list | tuple):
            raise ValueError("rate_limit.exclude_paths must be a list of paths")
        exclude_paths = tuple(str(path) for path in exclude_paths_raw)
        fail_open = _parse_bool(data.get("fail_open", True), field="rate_limit.fail_open")

        # How many proxies of YOUR OWN sit in front of the app and append to
        # X-Forwarded-For. Only that many entries, counted from the right, were
        # written by infrastructure you control; everything to the left of them came
        # from the caller and is forgeable. See keying._client_ip.
        trusted_proxy_hops = int(data.get("trusted_proxy_hops", 1))
        trusted_proxies = _parse_networks(data.get("trusted_proxies", []))

        # Validation
        if by not in ("ip", "global", "user"):
            raise ValueError(f"rate_limit.by must be 'ip', 'user', or 'global', got '{by}'")
        if requests <= 0:
            raise ValueError("rate_limit.requests must be a positive integer")
        if window <= 0:
            raise ValueError("rate_limit.window must be a positive integer")
        if trusted_proxy_hops < 1:
            raise ValueError("rate_limit.trusted_proxy_hops must be >= 1")
        if enabled and trusted_proxy_headers and not trusted_proxies:
            logger.warning(
                "rate_limit.trusted_proxy_headers is on without rate_limit.trusted_proxies; "
                "X-Forwarded-For is trusted only from loopback and private networks. List "
                "your proxy's addresses in trusted_proxies (or 0.0.0.0/0 and ::/0 if nothing "
                "can reach the app except through it)."
            )

        return cls(
            enabled=enabled,
            requests=requests,
            window=window,
            by=by,
            exclude_paths=exclude_paths,
            trusted_proxy_headers=trusted_proxy_headers,
            trusted_proxy_hops=trusted_proxy_hops,
            fail_open=fail_open,
            trusted_proxies=trusted_proxies,
        )
