import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from injectq import InjectQ
from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import HTTPConnection, Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from tenxgraph.core.exceptions import StaleStateError

from tenxgraph_api.src.app.core.exceptions.server_errors import UnhandledErrorMiddleware
from tenxgraph_api.src.app.core.middleware.rate_limit import RateLimitMiddleware
from tenxgraph_api.src.app.core.middleware.rate_limit.keying import _client_ip
from tenxgraph_api.src.app.core.middleware.request_limits import RequestSizeLimitMiddleware
from tenxgraph_api.src.app.core.middleware.security_headers import SecurityHeadersMiddleware
from tenxgraph_api.src.app.core.request_context import RequestContext, request_context_var

from .graph_config import GraphConfig, RateLimitConfig
from .metrics import setup_metrics
from .observability import (  # noqa: F401  (re-exported: tests and the worker import them from here)
    _attach_otel_publisher,
    _excluded_urls,
    _instrument_fastapi,
    _observability_publishers,
    _setup_observability,
    _setup_otel,
    _setup_otel_metrics,
    _user_id_mode,
    setup_otel_provider,
)
from .sentry_config import init_sentry
from .settings import get_settings, logger


class HealthCheckAwareTrustedHostMiddleware(TrustedHostMiddleware):
    """``TrustedHostMiddleware`` that lets the health probes through from any Host.

    Docker and Kubernetes probes call ``/ping`` and ``/health/ready`` on ``localhost`` or the
    pod IP, which are never in ``ALLOWED_HOST``, so the probe would fail and the container would
    be restarted. Both return only status words and read nothing from the request, so skipping
    the Host check there is safe. ``open_paths`` adds more such paths (the Prometheus scrape
    route, scraped by pod IP or service name).
    """

    def __init__(self, app, *, open_paths: tuple[str, ...] = (), **kwargs: Any) -> None:
        super().__init__(app, **kwargs)
        self.open_paths = (*HEALTH_PROBE_PATHS, *open_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and _is_health_check(scope, self.open_paths):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


# Multipart framing (boundaries, part headers, the filename field) on top of the file bytes.
UPLOAD_MULTIPART_OVERHEAD_BYTES = 64 * 1024


def _upload_path_limits(max_request_size: int) -> dict[str, int]:
    """A body limit for the upload route that fits ``MEDIA_MAX_SIZE_MB``, if that is larger."""
    from tenxgraph_api.src.app.core.config.media_settings import get_media_settings

    media_bytes = int(get_media_settings().MEDIA_MAX_SIZE_MB * 1024 * 1024)
    upload_limit = media_bytes + UPLOAD_MULTIPART_OVERHEAD_BYTES
    if upload_limit <= max_request_size:
        return {}
    return {"/v1/files/upload": upload_limit}


# Liveness and readiness probes come from the orchestrator on localhost or the pod IP.
HEALTH_PROBE_PATHS = ("/ping", "/health/ready")


def _is_health_check(scope: Scope, paths: tuple[str, ...] = HEALTH_PROBE_PATHS) -> bool:
    path = scope.get("path", "")
    root_path = (scope.get("root_path") or "").rstrip("/")
    return any(path in (probe, f"{root_path}{probe}") for probe in paths)


# Paths that should be excluded from GZip compression (streaming endpoints)
GZIP_EXCLUDED_PATHS = frozenset({"/v1/graph/stream"})


class AllowlistCORSMiddleware(CORSMiddleware):
    """``CORSMiddleware`` that grants private-network access only to allowed origins.

    Starlette adds ``Access-Control-Allow-Private-Network: true`` to every preflight that asks
    for it, including one it refuses for its origin. That refusal still stands (400, no
    ``Access-Control-Allow-Origin``), but an origin outside ``ORIGINS`` should not get the grant.
    """

    def preflight_response(self, request_headers: Headers) -> Response:
        response = super().preflight_response(request_headers)
        if not self.is_allowed_origin(origin=request_headers["origin"]):
            del response.headers["Access-Control-Allow-Private-Network"]
        return response


class InsecureCorsConfigError(RuntimeError):
    """Raised when production is configured with wildcard CORS + credentials."""


def _resolve_cors_policy(settings) -> tuple[list[str], bool]:
    """Decide the CORS origins/credentials policy, failing closed in production.

    ``ORIGINS="*"`` on its own is a legitimate choice for a public, token-less
    API, and a permissive default is normal in development. The dangerous case is
    wildcard origins *combined with credentials*: Starlette reflects the caller's
    Origin header back alongside ``Access-Control-Allow-Credentials: true``, which
    turns every origin into a trusted, credentialed one. Warning about it (the old
    behaviour) still served it.

    In production that combination is refused outright, with two ways forward:
    set explicit ``ORIGINS``, or set ``CORS_ALLOW_CREDENTIALS=false``.
    """
    origins = [o.strip() for o in settings.ORIGINS.split(",") if o.strip()]
    allow_credentials = bool(getattr(settings, "CORS_ALLOW_CREDENTIALS", True))
    is_wildcard = "*" in origins
    is_production = str(getattr(settings, "MODE", "development")).lower() == "production"

    if is_wildcard and allow_credentials:
        if is_production:
            raise InsecureCorsConfigError(
                "Refusing to start: CORS is configured with ORIGINS='*' and "
                "credentials enabled while MODE=production. This reflects any "
                "caller's Origin back with Access-Control-Allow-Credentials: true, "
                "making every origin a trusted one. Fix by either:\n"
                "  1. setting ORIGINS to an explicit comma-separated list, or\n"
                "  2. setting CORS_ALLOW_CREDENTIALS=false for a public, "
                "non-credentialed API."
            )
        logger.warning(
            "CORS is wildcard ('*') with credentials enabled. This is accepted in "
            "development but will refuse to start when MODE=production."
        )

    return origins, allow_credentials


async def _stale_state_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return 409 Conflict when a run loses the checkpointer's version check.

    A ``StaleStateError`` means another execution committed a newer state for the
    same thread while this one was running, so persisting would silently discard
    that work. This is a genuine conflict, not a server fault: surface it as 409
    so the client can reload the thread and retry instead of seeing a 500.
    """
    logger.warning("State conflict on concurrent run: %s", exc)
    context = getattr(exc, "context", {}) or {}
    return JSONResponse(
        status_code=409,
        content={
            "error": "state_conflict",
            "detail": (
                "This thread was updated by another run while yours was in flight. "
                "Reload the thread and retry."
            ),
            "thread_id": context.get("thread_id"),
        },
    )


class SelectiveGZipMiddleware:
    """
    GZip middleware that excludes certain paths from compression.

    This is necessary because streaming endpoints need to send data
    immediately without buffering, but GZipMiddleware buffers the
    entire response before compressing.
    """

    def __init__(self, app: ASGIApp, minimum_size: int = 1000):
        self.app = app
        self.gzip_app = GZipMiddleware(app, minimum_size=minimum_size)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] in GZIP_EXCLUDED_PATHS:
            # Skip GZip for excluded paths - pass through directly
            await self.app(scope, receive, send)
        else:
            # Apply GZip for all other paths
            await self.gzip_app(scope, receive, send)


class RequestIDMiddleware:
    """Give every HTTP request a unique id and timestamp.

    Both go into request state (``request.state.request_id`` / ``.timestamp``, read by the
    error responses and logs) and onto the response as ``X-Request-ID`` / ``X-Timestamp``.

    Pure ASGI: it edits the ``http.response.start`` message on the way out, so it adds no
    per-request task and never buffers a streaming body (``BaseHTTPMiddleware`` does both).
    """

    def __init__(self, app: ASGIApp, rate_limit_config: RateLimitConfig | None = None) -> None:
        self.app = app
        self.rate_limit_config = rate_limit_config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            # Same request context as HTTP, but no headers: the handshake send path is untouched.
            scope.setdefault("state", {})["request_id"] = request_id = str(uuid.uuid4())
            token = self._bind_context(scope, request_id, "ws")
            try:
                await self.app(scope, receive, send)
            finally:
                request_context_var.reset(token)
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = str(uuid.uuid4())
        timestamp = datetime.now().isoformat()
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["timestamp"] = timestamp
        logger.debug(f"Requesting: Request ID: {request_id}, Timestamp: {timestamp}")

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["X-Timestamp"] = timestamp
            await send(message)

        token = self._bind_context(scope, request_id, "http")
        try:
            await self.app(scope, receive, send_with_id)
        finally:
            request_context_var.reset(token)
        logger.debug(f"Response: Request ID: {request_id}, Timestamp: {timestamp}")

    def _bind_context(self, scope: Scope, request_id: str, transport: str) -> Any:
        """Set the request context; client_ip is the address the rate limiter keys on."""
        config = self.rate_limit_config
        if config is not None:
            client_ip = _client_ip(HTTPConnection(scope), config)
        else:
            client_ip = scope["client"][0] if scope.get("client") else None
        return request_context_var.set(
            RequestContext(request_id=request_id, transport=transport, client_ip=client_ip)
        )


def _add_metrics_route(app: FastAPI, path: str) -> None:
    """Serve the Prometheus registry at ``path``. Public like /ping: scrapers send no tokens,
    so keep it on the private network."""
    from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

    def scrape() -> Response:
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    app.add_api_route(path, scrape, methods=["GET"], include_in_schema=False)
    app.state.scrape_route = app.routes[-1]


def _setup_metrics(app: FastAPI, graph_config: GraphConfig | None):
    """Start the ``metrics`` block (when on) and return the MeterProvider, else None."""
    metrics_cfg = graph_config.metrics if graph_config is not None else None
    if metrics_cfg is None or not metrics_cfg.enabled:
        return None
    provider = setup_metrics(metrics_cfg, role="api")
    if provider is None:
        return None
    app.state.meter_provider = provider
    if metrics_cfg.scrape_path:
        try:
            _add_metrics_route(app, metrics_cfg.scrape_path)
        except ImportError:
            logger.warning("Metrics scrape route skipped: install '10xgraph-api[metrics]'")
    return provider


def setup_middleware(
    app: FastAPI,
    graph_config: GraphConfig | None = None,
    container: InjectQ | None = None,
):
    """
    Set up middleware for the FastAPI application.

    Args:
        app (FastAPI): The FastAPI application instance.
        graph_config (GraphConfig | None): Optional graph configuration used to
            enable dynamic rate limiting from ``10xgraph.json``.

    Middleware:
        - CORS: Configured based on settings.ORIGINS.
        - TrustedHost: Configured with allowed hosts from settings.ALLOWED_HOST.
        - GZip: Applied with a minimum size of 1000 bytes (excludes streaming endpoints).
        - RateLimit: Applied when ``cache_store.rate_limit`` is set in ``10xgraph.json``.
    """
    settings = get_settings()

    # A concurrent run on the same thread is a conflict (409), not a 500.
    app.add_exception_handler(StaleStateError, _stale_state_handler)

    meter_provider = _setup_metrics(app, graph_config)
    scrape_path = graph_config.metrics.scrape_path if graph_config is not None else None

    instrumented = False
    if settings.OTEL_ENABLED:
        instrumented = _setup_otel(app, settings, meter_provider, scrape_path)
        if container is not None:
            _attach_otel_publisher(container, settings, graph_config)
    # HTTP metrics once: here unless the legacy path instrumented (it can fail, e.g. no endpoint).
    if meter_provider is not None and not instrumented:
        _instrument_fastapi(
            app, tracing=False, meter_provider=meter_provider, scrape_path=scrape_path
        )

    # Declarative Logfire/LangSmith wiring from 10xgraph.json (before compile).
    _setup_observability(container, graph_config)

    # Starlette runs the LAST added middleware FIRST, so these are added innermost first. The
    # resulting order, outermost first:
    #
    #   CORS -> request id -> security headers -> trusted host -> rate limit -> size limit
    #   -> gzip -> unhandled-error catch -> app
    #
    # Anything an inner layer answers on its own (429, 413, a bad Host) still passes back out
    # through CORS, request id and security headers. With CORS innermost, as before, a browser
    # could not read those responses at all.
    origins, allow_credentials = _resolve_cors_policy(settings)

    # Innermost: an exception nobody handled becomes the standard JSON error here, so it still
    # passes out through the layers below (request id, security headers, CORS).
    app.add_middleware(UnhandledErrorMiddleware)

    # Exclude streaming endpoints from compression: they must send data as it is produced.
    app.add_middleware(SelectiveGZipMiddleware, minimum_size=1000)

    # Request size limit (protects against DoS via large payloads). The upload route gets room
    # for MEDIA_MAX_SIZE_MB, which the route itself enforces on the file.
    app.add_middleware(
        RequestSizeLimitMiddleware,
        max_size=settings.MAX_REQUEST_SIZE,
        path_limits=_upload_path_limits(settings.MAX_REQUEST_SIZE),
    )

    # Apply rate limiting only when configured in 10xgraph.json
    if graph_config is not None:
        rate_limit_config = graph_config.rate_limit
        if rate_limit_config is not None:
            if scrape_path:  # scrapes come every few seconds from one address
                rate_limit_config = replace(
                    rate_limit_config,
                    exclude_paths=(*rate_limit_config.exclude_paths, scrape_path),
                )
            # The counters live in the cache store, bound in the container at startup.
            app.add_middleware(
                RateLimitMiddleware,
                config=rate_limit_config,
                container=container,
            )
            logger.info(
                "Rate limiting enabled: %d req/%ds, by=%s, exclude_paths=%s, "
                "trusted_proxy_headers=%s, fail_open=%s",
                rate_limit_config.requests,
                rate_limit_config.window,
                rate_limit_config.by,
                rate_limit_config.exclude_paths or "(none)",
                "on" if rate_limit_config.trusted_proxy_headers else "off",
                rate_limit_config.fail_open,
            )

    app.add_middleware(
        HealthCheckAwareTrustedHostMiddleware,
        open_paths=(scrape_path,) if scrape_path else (),
        allowed_hosts=[h.strip() for h in settings.ALLOWED_HOST.split(",") if h.strip()],
    )

    if settings.SECURITY_HEADERS_ENABLED:
        app.add_middleware(
            SecurityHeadersMiddleware,
            enable_hsts=settings.HSTS_ENABLED,
            hsts_max_age=settings.HSTS_MAX_AGE,
            hsts_include_subdomains=settings.HSTS_INCLUDE_SUBDOMAINS,
            hsts_preload=settings.HSTS_PRELOAD,
            frame_options=settings.FRAME_OPTIONS,
            content_type_options=settings.CONTENT_TYPE_OPTIONS,
            xss_protection=settings.XSS_PROTECTION,
            referrer_policy=settings.REFERRER_POLICY,
            permissions_policy=settings.PERMISSIONS_POLICY,
            csp_policy=settings.CSP_POLICY,
        )

    app.add_middleware(
        RequestIDMiddleware,
        rate_limit_config=graph_config.rate_limit if graph_config is not None else None,
    )

    app.add_middleware(
        AllowlistCORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
        # Answer the Private Network Access preflight the hosted playground sends to a local server.
        allow_private_network=True,
    )

    logger.debug("Middleware set up")

    # Initialize Sentry
    init_sentry(settings)
