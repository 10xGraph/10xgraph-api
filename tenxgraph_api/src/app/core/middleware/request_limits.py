"""Request size limit middleware for DoS protection."""

from typing import Any

from fastapi import status
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.audit import audit_event


class RequestBodyTooLarge(HTTPException):
    """Raised from ``receive`` once a streamed body passes the limit.

    It is an ``HTTPException`` because FastAPI re-raises those when they come from reading
    the body, instead of turning them into a generic 400, so the client gets a 413.
    """

    def __init__(self, max_size: int) -> None:
        super().__init__(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Request body too large. Maximum size is {max_size / (1024 * 1024):.1f}MB",
        )


class RequestSizeLimitMiddleware:
    """Enforce a maximum request body size, whether or not ``Content-Length`` is sent.

    A declared ``Content-Length`` over the limit is rejected before any body is read. Bodies
    without one (``Transfer-Encoding: chunked``) are counted as they stream in and cut off as
    soon as they pass the limit. That matters because FastAPI reads and parses the whole body,
    including spooling multipart uploads to disk, before auth dependencies run.

    This is a pure ASGI middleware: ``BaseHTTPMiddleware`` cannot see body chunks.

    Args:
        app: The ASGI application
        max_size: Maximum request body size in bytes (default: 10MB)
        path_limits: Per-path limits that replace ``max_size``, e.g. a larger one for the
            file upload route so ``MEDIA_MAX_SIZE_MB`` is reachable.
    """

    def __init__(
        self,
        app: ASGIApp,
        max_size: int = 10 * 1024 * 1024,
        path_limits: dict[str, int] | None = None,
    ) -> None:
        self.app = app
        self.max_size = max_size
        self.max_size_mb = max_size / (1024 * 1024)
        self.path_limits = dict(path_limits or {})

    def _limit_for(self, scope: Scope) -> int:
        path = scope.get("path", "")
        root_path = (scope.get("root_path") or "").rstrip("/")
        if root_path and path.startswith(root_path):
            path = path[len(root_path) :] or "/"
        return self.path_limits.get(path, self.max_size)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        max_size = self._limit_for(scope)
        max_size_mb = max_size / (1024 * 1024)

        content_length = Headers(scope=scope).get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                # A malformed Content-Length is a client error, not a server 500.
                await self._error(
                    scope,
                    send,
                    status.HTTP_400_BAD_REQUEST,
                    {"code": "INVALID_CONTENT_LENGTH", "message": "Invalid Content-Length header."},
                )
                return
            if declared > max_size:
                logger.warning(
                    f"Request rejected: size {declared} bytes exceeds limit of "
                    f"{max_size} bytes ({max_size_mb:.1f}MB)"
                )
                await audit_event("input", "denied", reason="request_too_large")
                await self._too_large(scope, send, max_size)
                return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_size:
                    logger.warning(
                        f"Request rejected: streamed body passed the limit of "
                        f"{max_size} bytes ({max_size_mb:.1f}MB)"
                    )
                    await audit_event("input", "denied", reason="request_too_large")
                    raise RequestBodyTooLarge(max_size)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except RequestBodyTooLarge:
            # Normally the app's HTTPException handling answers with the 413. This covers a
            # body read outside FastAPI's request handling (e.g. by another middleware).
            if response_started:
                raise
            await self._too_large(scope, send, max_size)

    async def _too_large(self, scope: Scope, send: Send, max_size: int) -> None:
        max_size_mb = max_size / (1024 * 1024)
        await self._error(
            scope,
            send,
            status.HTTP_413_CONTENT_TOO_LARGE,
            {
                "code": "REQUEST_TOO_LARGE",
                "message": f"Request body too large. Maximum size is {max_size_mb:.1f}MB",
                "max_size_bytes": max_size,
                "max_size_mb": max_size_mb,
            },
        )

    @staticmethod
    async def _error(scope: Scope, send: Send, status_code: int, error: dict[str, Any]) -> None:
        state = scope.get("state") or {}
        response = JSONResponse(
            status_code=status_code,
            content={
                "error": error,
                "metadata": {
                    "request_id": state.get("request_id", "unknown"),
                    "status": "error",
                },
            },
        )
        await response(scope, _drain_receive, send)


async def _drain_receive() -> Message:
    return {"type": "http.disconnect"}
