import time

from fastapi import status
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.config.graph_config import RateLimitConfig
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe

from .base import BaseRateLimitBackend, RateLimitDecision
from .keying import client_key_for


# Liveness/readiness probes hit /ping. Limiting it (or failing closed on a backend outage)
# would make the orchestrator restart healthy pods.
HEALTH_CHECK_PATHS = frozenset({"/ping", "/health/ready"})


class RateLimitMiddleware:
    """Backend-agnostic rate-limit middleware.

    Pure ASGI: it answers 429 (limit hit) or 503 (backend unreachable) itself, or edits the
    ``http.response.start`` message on the way out, so it adds no per-request task and never
    buffers a streaming body.
    """

    def __init__(
        self,
        app: ASGIApp,
        config: RateLimitConfig,
        backend: BaseRateLimitBackend,
    ) -> None:
        self.app = app
        self.config = config
        self.backend = backend
        self._exclude = frozenset(config.exclude_paths) | HEALTH_CHECK_PATHS

    def _client_key(self, request: Request) -> str:
        return client_key_for(request, self.config)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path", "") in self._exclude:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        key = self._client_key(request)
        decision = await self.backend.check(
            key,
            limit=self.config.requests,
            window=self.config.window,
        )
        reset_at_epoch = int(time.time()) + decision.reset_after

        if not decision.allowed and decision.unavailable:
            # The backend failed and fails closed: an outage, not the client's fault. The
            # backend logs the error itself, so this does not log per request.
            await self._unavailable(decision, scope)(scope, receive, send)
            return

        if not decision.allowed:
            request_id = (scope.get("state") or {}).get("request_id", "unknown")
            logger.warning(
                "Rate limit exceeded for %s on %s %s",
                log_safe(key),
                log_safe(request.method),
                log_safe(request.url.path),
            )
            response = JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "error": {
                        "code": "RATE_LIMIT_EXCEEDED",
                        "message": (
                            f"Too many requests. Limit: {self.config.requests} "
                            f"per {self.config.window}s. "
                            f"Retry after {decision.reset_after}s."
                        ),
                        "limit": self.config.requests,
                        "window_seconds": self.config.window,
                        "retry_after_seconds": decision.reset_after,
                    },
                    "metadata": {
                        "request_id": request_id,
                        "status": "error",
                    },
                },
                headers={
                    "Retry-After": str(decision.reset_after),
                    "X-RateLimit-Limit": str(self.config.requests),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(reset_at_epoch),
                    "X-RateLimit-Reset-After": str(decision.reset_after),
                },
            )
            await response(scope, receive, send)
            return

        limit_headers = {
            "X-RateLimit-Limit": str(self.config.requests),
            "X-RateLimit-Remaining": str(decision.remaining),
            "X-RateLimit-Reset": str(reset_at_epoch),
            "X-RateLimit-Reset-After": str(decision.reset_after),
        }

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in limit_headers.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    def _unavailable(decision: RateLimitDecision, scope: Scope) -> JSONResponse:
        retry_after = max(decision.reset_after, 1)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "RATE_LIMIT_UNAVAILABLE",
                    "message": "Rate limiter unavailable: its backend could not be reached.",
                    "retry_after_seconds": retry_after,
                },
                "metadata": {
                    "request_id": (scope.get("state") or {}).get("request_id", "unknown"),
                    "status": "error",
                },
            },
            headers={"Retry-After": str(retry_after)},
        )
