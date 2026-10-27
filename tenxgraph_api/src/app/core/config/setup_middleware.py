import uuid
from datetime import datetime
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from injectq import InjectQ
from starlette.datastructures import MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tenxgraph_api.src.app.core.middleware.rate_limit import RateLimitMiddleware, build_backend
from tenxgraph_api.src.app.core.middleware.request_limits import RequestSizeLimitMiddleware
from tenxgraph_api.src.app.core.middleware.security_headers import SecurityHeadersMiddleware

from .graph_config import GraphConfig
from .sentry_config import init_sentry
from .settings import get_settings, logger


class HealthCheckAwareTrustedHostMiddleware(TrustedHostMiddleware):
    """``TrustedHostMiddleware`` that lets the health probes through from any Host.

    Docker and Kubernetes probes call ``/ping`` and ``/health/ready`` on ``localhost`` or the
    pod IP, which are never in ``ALLOWED_HOST``, so the probe would fail and the container would
    be restarted. Both return only status words and read nothing from the request, so skipping
    the Host check there is safe.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and _is_health_check(scope):
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


def _is_health_check(scope: Scope) -> bool:
    path = scope.get("path", "")
    root_path = (scope.get("root_path") or "").rstrip("/")
    return any(path in (probe, f"{root_path}{probe}") for probe in HEALTH_PROBE_PATHS)


# Paths that should be excluded from GZip compression (streaming endpoints)
GZIP_EXCLUDED_PATHS = frozenset({"/v1/graph/stream"})

# StaleStateError is raised by checkpointers that do optimistic concurrency
# control on state writes. It was added in a later core release, and this package
# supports 10xgraph>=0.7.0, so treat it as optional: when the installed
# core does not expose it, we simply do not register the 409 handler.
try:
    from tenxgraph.core.exceptions import StaleStateError
except ImportError:  # pragma: no cover - depends on installed core version
    StaleStateError = None  # type: ignore[assignment]


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

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
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

        await self.app(scope, receive, send_with_id)
        logger.debug(f"Response: Request ID: {request_id}, Timestamp: {timestamp}")


def _user_id_mode(graph_config: GraphConfig | None) -> str:
    """``observability.user_id_mode`` from ``10xgraph.json``: how traces carry the user id."""
    obs_cfg = graph_config.observability if graph_config is not None else None
    return str((obs_cfg or {}).get("user_id_mode") or "raw")


def _make_otel_publisher(publisher_cls: Any, level: Any, user_id_mode: str) -> Any:
    """Build an ``OtelPublisher``, passing ``user_id_mode`` only when it is not the default.

    Cores older than the release that added ``user_id_mode`` do not accept it; they keep the raw
    id, which is logged so the operator knows the setting had no effect.
    """
    if user_id_mode == "raw":
        return publisher_cls(level=level)
    try:
        return publisher_cls(level=level, user_id_mode=user_id_mode)
    except TypeError:
        logger.warning(
            "observability.user_id_mode=%r needs a newer 10xgraph core; traces keep the raw id.",
            user_id_mode,
        )
        return publisher_cls(level=level)


def _attach_otel_publisher(
    container: InjectQ, settings, graph_config: GraphConfig | None = None
) -> None:
    """Wire OtelPublisher into the container when OTEL_ENABLED=true.

    Merges with any publisher already bound in the container:
      - None bound         → bind OtelPublisher directly
      - CompositePublisher → add OtelPublisher to it
      - Single publisher   → wrap both in a new CompositePublisher
    """
    try:
        from tenxgraph.runtime.publisher.base_publisher import BasePublisher
        from tenxgraph.runtime.publisher.composite_publisher import CompositePublisher
        from tenxgraph.runtime.publisher.otel_publisher import ObservabilityLevel, OtelPublisher
    except ImportError:
        logger.warning(
            "OTEL_ENABLED=true but opentelemetry packages are not installed. "
            "Install with: pip install '10xgraph[otel]'"
        )
        return

    try:
        level = ObservabilityLevel(settings.OTEL_LEVEL.lower())
    except ValueError:
        logger.warning(
            "Invalid OTEL_LEVEL=%r, falling back to 'standard'. Valid: spans, standard, full",
            settings.OTEL_LEVEL,
        )
        level = ObservabilityLevel.STANDARD

    otel_publisher = _make_otel_publisher(OtelPublisher, level, _user_id_mode(graph_config))
    existing = container.try_get(BasePublisher)

    if existing is None:
        container.bind_instance(BasePublisher, otel_publisher)
        logger.info("OTEL: OtelPublisher bound (level=%s)", level)
    elif isinstance(existing, CompositePublisher):
        existing.add_publisher(otel_publisher)
        logger.info("OTEL: OtelPublisher added to existing CompositePublisher (level=%s)", level)
    else:
        container.bind_instance(BasePublisher, CompositePublisher([existing, otel_publisher]))
        logger.info(
            "OTEL: wrapped %s + OtelPublisher in CompositePublisher (level=%s)",
            type(existing).__name__,
            level,
        )


def _setup_observability(container: InjectQ | None, graph_config: GraphConfig | None) -> None:
    """Wire Logfire/LangSmith from the ``observability`` block of ``10xgraph.json``.

    Configures the tracer provider(s)/exporters and binds an ``OtelPublisher``
    into the DI container. This runs at app-construction time, before the graph
    is loaded and compiled — the only reliable attach point, because InjectQ
    freezes the ``BasePublisher`` binding at ``container.compile()`` (which runs
    inside ``StateGraph.compile()``). The core ``StateGraph.compile()`` guard
    preserves a publisher already present in the container instead of clobbering
    it with ``None``.

    Secrets stay in the environment (``LOGFIRE_TOKEN``, ``LANGSMITH_API_KEY``);
    only non-secret settings live in ``10xgraph.json``.
    """
    if container is None or graph_config is None:
        return

    obs_cfg = graph_config.observability
    if not obs_cfg:
        return

    logfire_on = bool((obs_cfg.get("logfire") or {}).get("enabled", False))
    langsmith_on = bool((obs_cfg.get("langsmith") or {}).get("enabled", False))
    if not logfire_on and not langsmith_on:
        return

    try:
        from tenxgraph.runtime.publisher.base_publisher import BasePublisher
        from tenxgraph.runtime.publisher.composite_publisher import CompositePublisher
        from tenxgraph.runtime.publisher.exporters import setup_observability
        from tenxgraph.runtime.publisher.otel_publisher import ObservabilityLevel, OtelPublisher
    except ImportError:
        logger.warning(
            "observability is configured but required packages are not installed. "
            "Install with: pip install '10xgraph[observability]'"
        )
        return

    # Configure the provider(s)/exporter(s) only. graph=None means no publisher
    # is attached here; we bind it into the container ourselves below so it is
    # in place before the graph compiles.
    try:
        setup_observability(None, obs_cfg)
    except (ImportError, ValueError) as exc:
        logger.warning("Observability setup skipped: %s", exc)
        return

    try:
        level = ObservabilityLevel(str(obs_cfg.get("level", "standard")).lower())
    except ValueError:
        logger.warning(
            "Invalid observability level=%r, falling back to 'standard'.",
            obs_cfg.get("level"),
        )
        level = ObservabilityLevel.STANDARD

    otel_publisher = _make_otel_publisher(OtelPublisher, level, _user_id_mode(graph_config))
    existing = container.try_get(BasePublisher)

    if existing is None:
        container.bind_instance(BasePublisher, otel_publisher)
    elif isinstance(existing, CompositePublisher):
        existing.add_publisher(otel_publisher)
    else:
        container.bind_instance(BasePublisher, CompositePublisher([existing, otel_publisher]))

    logger.info(
        "Observability enabled (logfire=%s, langsmith=%s, level=%s)",
        logfire_on,
        langsmith_on,
        level,
    )


def _setup_otel(app: FastAPI, settings) -> None:
    """Configure OpenTelemetry tracing and instrument the FastAPI app.

    Reads OTEL_EXPORTER_OTLP_ENDPOINT from settings. If unset, falls back to
    the standard OTEL_EXPORTER_OTLP_ENDPOINT env var (honoured automatically
    by the OTLP exporter). A ConsoleSpanExporter is used when no endpoint is
    configured and the app is not in production, so traces are visible locally
    without any collector. In production with no endpoint nothing is set up and a warning
    is logged.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning(
            "OTEL_ENABLED=true but opentelemetry packages are not installed. "
            "Install with: pip install '10xgraph-api[otel]'"
        )
        return

    resource = Resource.create({"service.name": settings.OTEL_SERVICE_NAME})
    provider = TracerProvider(resource=resource)

    endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT
    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
            logger.info("OTEL: exporting traces to %s", endpoint)
        except ImportError:
            logger.warning(
                "OTEL_EXPORTER_OTLP_ENDPOINT is set but opentelemetry-exporter-otlp "
                "is not installed. Install with: pip install '10xgraph-api[otel]'"
            )
            return
    elif settings.MODE == "production":
        # Printing spans to stdout would put request URLs and, at OTEL_LEVEL=full, message
        # content in the container logs, so production exports nowhere instead.
        logger.warning(
            "OTEL_ENABLED=true but OTEL_EXPORTER_OTLP_ENDPOINT is not set; traces are not "
            "exported. Console span output is development-only. Set the endpoint to export."
        )
        return
    else:
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor

        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
        logger.info("OTEL: no endpoint configured, printing spans to console")

    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
    logger.info("OTEL: FastAPI instrumentation active (service=%s)", settings.OTEL_SERVICE_NAME)


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
        - RateLimit: Applied when ``rate_limit`` is configured in ``10xgraph.json``.
    """
    settings = get_settings()

    # A concurrent run on the same thread is a conflict (409), not a 500.
    if StaleStateError is not None:
        app.add_exception_handler(StaleStateError, _stale_state_handler)

    if settings.OTEL_ENABLED:
        _setup_otel(app, settings)
        if container is not None:
            _attach_otel_publisher(container, settings, graph_config)

    # Declarative Logfire/LangSmith wiring from 10xgraph.json (before compile).
    _setup_observability(container, graph_config)

    # Starlette runs the LAST added middleware FIRST, so these are added innermost first. The
    # resulting order, outermost first:
    #
    #   CORS -> request id -> security headers -> trusted host -> rate limit -> size limit
    #   -> gzip -> app
    #
    # Anything an inner layer answers on its own (429, 413, a bad Host) still passes back out
    # through CORS, request id and security headers. With CORS innermost, as before, a browser
    # could not read those responses at all.
    origins, allow_credentials = _resolve_cors_policy(settings)

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
            backend = build_backend(rate_limit_config, container=container)
            # Store on app.state so lifespan can close it cleanly
            app.state.rate_limit_backend = backend
            app.add_middleware(
                RateLimitMiddleware,
                config=rate_limit_config,
                backend=backend,
            )
            logger.info(
                "Rate limiting enabled: backend=%s, %d req/%ds, by=%s, "
                "exclude_paths=%s, trusted_proxy_headers=%s",
                rate_limit_config.backend,
                rate_limit_config.requests,
                rate_limit_config.window,
                rate_limit_config.by,
                rate_limit_config.exclude_paths or "(none)",
                "on" if rate_limit_config.trusted_proxy_headers else "off",
            )

    app.add_middleware(
        HealthCheckAwareTrustedHostMiddleware,
        allowed_hosts=settings.ALLOWED_HOST.split(","),
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

    app.add_middleware(RequestIDMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    logger.debug("Middleware set up")

    # Initialize Sentry
    init_sentry(settings)
