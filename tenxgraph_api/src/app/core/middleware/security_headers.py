"""
Security Headers Middleware

This middleware adds standard security headers to HTTP responses to protect
against common web vulnerabilities.

Headers Added:
- X-Content-Type-Options: Prevents MIME-type sniffing
- X-Frame-Options: Prevents clickjacking attacks
- X-XSS-Protection: Enables XSS filtering (legacy browsers)
- Strict-Transport-Security: Enforces HTTPS (if HTTPS is detected)
- Content-Security-Policy: Controls resource loading
- Referrer-Policy: Controls referrer information
- Permissions-Policy: Controls browser features

Configuration:
Configure via environment variables or settings:
- SECURITY_HEADERS_ENABLED: Enable/disable middleware (default: true)
- HSTS_MAX_AGE: HSTS max-age in seconds (default: 31536000 = 1 year)
- CSP_POLICY: Custom CSP policy (default: strict policy)
"""

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class SecurityHeadersMiddleware:
    """
    Middleware to add security headers to responses.

    This middleware enhances security by adding standard security headers
    that protect against common web vulnerabilities.

    Pure ASGI: it edits the ``http.response.start`` message on the way out, so it adds no
    per-request task and never buffers a streaming body (``BaseHTTPMiddleware`` does both).
    """

    def __init__(  # noqa: PLR0913
        self,
        app: ASGIApp,
        enable_hsts: bool = True,
        hsts_max_age: int = 31536000,  # 1 year
        hsts_include_subdomains: bool = True,
        hsts_preload: bool = False,
        frame_options: str = "DENY",
        content_type_options: str = "nosniff",
        xss_protection: str = "1; mode=block",
        referrer_policy: str = "strict-origin-when-cross-origin",
        permissions_policy: str | None = None,
        csp_policy: str | None = None,
    ):
        """
        Initialize security headers middleware.

        Args:
            app: ASGI application
            enable_hsts: Enable Strict-Transport-Security header
            hsts_max_age: HSTS max-age in seconds (default: 1 year)
            hsts_include_subdomains: Include subdomains in HSTS
            hsts_preload: Enable HSTS preload
            frame_options: X-Frame-Options value (DENY, SAMEORIGIN, or ALLOW-FROM)
            content_type_options: X-Content-Type-Options value
            xss_protection: X-XSS-Protection value
            referrer_policy: Referrer-Policy value
            permissions_policy: Permissions-Policy value (optional)
            csp_policy: Content-Security-Policy value (optional)
        """
        self.app = app
        self.enable_hsts = enable_hsts
        self.hsts_max_age = hsts_max_age
        self.hsts_include_subdomains = hsts_include_subdomains
        self.hsts_preload = hsts_preload
        self.frame_options = frame_options
        self.content_type_options = content_type_options
        self.xss_protection = xss_protection
        self.referrer_policy = referrer_policy
        self.permissions_policy = permissions_policy or self._default_permissions_policy()
        self.csp_policy = csp_policy or self._default_csp_policy()

    def _default_permissions_policy(self) -> str:
        """
        Get default Permissions-Policy header value.

        Returns:
            Default permissions policy string
        """
        return (
            "geolocation=(), microphone=(), camera=(), payment=(), usb=(), "
            "magnetometer=(), gyroscope=(), accelerometer=()"
        )

    def _default_csp_policy(self) -> str:
        """
        Get default Content-Security-Policy header value.

        Returns:
            Default CSP policy string
        """
        return (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com;"
            "img-src 'self' data: https:; "
            "font-src 'self' data: https://fonts.gstatic.com; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )

    def _build_hsts_header(self) -> str:
        """
        Build HSTS header value.

        Returns:
            HSTS header string
        """
        hsts_parts = [f"max-age={self.hsts_max_age}"]

        if self.hsts_include_subdomains:
            hsts_parts.append("includeSubDomains")

        if self.hsts_preload:
            hsts_parts.append("preload")

        return "; ".join(hsts_parts)

    def _headers_for(self, is_https: bool) -> list[tuple[str, str]]:
        """The headers this middleware adds, in order, for a plain or an HTTPS request."""
        headers = [
            ("X-Content-Type-Options", self.content_type_options),
            ("X-Frame-Options", self.frame_options),
            ("X-XSS-Protection", self.xss_protection),  # legacy browsers
            ("Referrer-Policy", self.referrer_policy),
            # API answers carry per-user data; a shared cache or the browser must not keep them.
            # A route that sets its own Cache-Control (a file download) keeps it.
            ("Cache-Control", "no-store"),
        ]
        if self.permissions_policy:
            headers.append(("Permissions-Policy", self.permissions_policy))
        if self.csp_policy:
            headers.append(("Content-Security-Policy", self.csp_policy))
        if self.enable_hsts and is_https:  # only meaningful over HTTPS
            headers.append(("Strict-Transport-Security", self._build_hsts_header()))
        return headers

    @staticmethod
    def _is_https(scope: Scope) -> bool:
        """Whether the request came over HTTPS, directly or through a TLS-terminating proxy."""
        if scope.get("scheme") == "https":
            return True
        forwarded_proto = Headers(scope=scope).get("x-forwarded-proto", "")
        return forwarded_proto.lower() == "https"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        defaults = self._headers_for(self._is_https(scope))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                # A header the route set itself wins: it knows its content better than a
                # global default does (e.g. the file download's sandbox CSP for uploads).
                for name, value in defaults:
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


def create_security_headers_middleware(
    enable_hsts: bool = True,
    hsts_max_age: int = 31536000,
    hsts_include_subdomains: bool = True,
    hsts_preload: bool = False,
    frame_options: str = "DENY",
    content_type_options: str = "nosniff",
    xss_protection: str = "1; mode=block",
    referrer_policy: str = "strict-origin-when-cross-origin",
    permissions_policy: str | None = None,
    csp_policy: str | None = None,
) -> type[SecurityHeadersMiddleware]:
    """
    Factory function to create SecurityHeadersMiddleware with configuration.

    This is a convenience function for creating middleware with custom settings.

    Args:
        enable_hsts: Enable HSTS header
        hsts_max_age: HSTS max-age in seconds
        hsts_include_subdomains: Include subdomains in HSTS
        hsts_preload: Enable HSTS preload
        frame_options: X-Frame-Options value
        content_type_options: X-Content-Type-Options value
        xss_protection: X-XSS-Protection value
        referrer_policy: Referrer-Policy value
        permissions_policy: Permissions-Policy value
        csp_policy: Content-Security-Policy value

    Returns:
        Configured SecurityHeadersMiddleware class
    """

    class ConfiguredSecurityHeadersMiddleware(SecurityHeadersMiddleware):
        def __init__(self, app):
            super().__init__(
                app,
                enable_hsts=enable_hsts,
                hsts_max_age=hsts_max_age,
                hsts_include_subdomains=hsts_include_subdomains,
                hsts_preload=hsts_preload,
                frame_options=frame_options,
                content_type_options=content_type_options,
                xss_protection=xss_protection,
                referrer_policy=referrer_policy,
                permissions_policy=permissions_policy,
                csp_policy=csp_policy,
            )

    return ConfiguredSecurityHeadersMiddleware
