"""
Tests for SecurityHeadersMiddleware

Tests cover:
- All security headers are added correctly
- HSTS header only added for HTTPS requests
- Configuration options work correctly
- Custom CSP and Permissions policies
- Middleware can be disabled
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tenxgraph_api.src.app.core.middleware.security_headers import (
    SecurityHeadersMiddleware,
    create_security_headers_middleware,
)


@pytest.fixture
def app():
    """Create a test FastAPI application."""
    app = FastAPI()

    @app.get("/test")
    async def test_endpoint():
        return {"message": "test"}

    return app


@pytest.fixture
def client_with_headers(app):
    """Create test client with security headers middleware."""
    app.add_middleware(SecurityHeadersMiddleware)
    return TestClient(app)


def test_basic_security_headers_added(client_with_headers):
    """Test that basic security headers are added to responses."""
    response = client_with_headers.get("/test")

    assert response.status_code == 200
    assert "X-Content-Type-Options" in response.headers
    assert response.headers["X-Content-Type-Options"] == "nosniff"

    assert "X-Frame-Options" in response.headers
    assert response.headers["X-Frame-Options"] == "DENY"

    assert "X-XSS-Protection" in response.headers
    assert response.headers["X-XSS-Protection"] == "1; mode=block"

    assert "Referrer-Policy" in response.headers
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_permissions_policy_added(client_with_headers):
    """Test that Permissions-Policy header is added."""
    response = client_with_headers.get("/test")

    assert "Permissions-Policy" in response.headers
    assert "geolocation=()" in response.headers["Permissions-Policy"]
    assert "microphone=()" in response.headers["Permissions-Policy"]
    assert "camera=()" in response.headers["Permissions-Policy"]


def test_csp_policy_added(client_with_headers):
    """Test that Content-Security-Policy header is added."""
    response = client_with_headers.get("/test")

    assert "Content-Security-Policy" in response.headers
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_hsts_not_added_for_http(client_with_headers):
    """Test that HSTS header is NOT added for HTTP requests."""
    response = client_with_headers.get("/test")

    # HSTS should not be present for HTTP requests
    assert "Strict-Transport-Security" not in response.headers


def test_hsts_added_for_https(app):
    """Test that HSTS header IS added for HTTPS requests."""
    app.add_middleware(SecurityHeadersMiddleware)
    client = TestClient(app, base_url="https://testserver")

    response = client.get("/test")

    # HSTS should be present for HTTPS requests
    assert "Strict-Transport-Security" in response.headers
    hsts = response.headers["Strict-Transport-Security"]
    assert "max-age=31536000" in hsts
    assert "includeSubDomains" in hsts


def test_hsts_with_x_forwarded_proto(app):
    """Test that HSTS is added when X-Forwarded-Proto is https (proxied requests)."""
    app.add_middleware(SecurityHeadersMiddleware)
    client = TestClient(app)

    response = client.get("/test", headers={"X-Forwarded-Proto": "https"})

    # HSTS should be present when proxied via HTTPS
    assert "Strict-Transport-Security" in response.headers


def test_custom_frame_options(app):
    """Test custom X-Frame-Options value."""
    app.add_middleware(
        SecurityHeadersMiddleware,
        frame_options="SAMEORIGIN",
    )
    client = TestClient(app)

    response = client.get("/test")

    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"


def test_custom_csp_policy(app):
    """Test custom Content-Security-Policy."""
    custom_csp = "default-src 'none'; script-src 'self'"
    app.add_middleware(
        SecurityHeadersMiddleware,
        csp_policy=custom_csp,
    )
    client = TestClient(app)

    response = client.get("/test")

    assert response.headers["Content-Security-Policy"] == custom_csp


def test_custom_permissions_policy(app):
    """Test custom Permissions-Policy."""
    custom_policy = "geolocation=*, camera=()"
    app.add_middleware(
        SecurityHeadersMiddleware,
        permissions_policy=custom_policy,
    )
    client = TestClient(app)

    response = client.get("/test")

    assert response.headers["Permissions-Policy"] == custom_policy


def test_hsts_disabled(app):
    """Test that HSTS can be disabled."""
    app.add_middleware(
        SecurityHeadersMiddleware,
        enable_hsts=False,
    )
    client = TestClient(app, base_url="https://testserver")

    response = client.get("/test")

    # HSTS should not be present even for HTTPS when disabled
    assert "Strict-Transport-Security" not in response.headers


def test_hsts_with_preload(app):
    """Test HSTS with preload option."""
    app.add_middleware(
        SecurityHeadersMiddleware,
        hsts_preload=True,
    )
    client = TestClient(app, base_url="https://testserver")

    response = client.get("/test")

    hsts = response.headers["Strict-Transport-Security"]
    assert "preload" in hsts


def test_hsts_without_subdomains(app):
    """Test HSTS without includeSubDomains."""
    app.add_middleware(
        SecurityHeadersMiddleware,
        hsts_include_subdomains=False,
    )
    client = TestClient(app, base_url="https://testserver")

    response = client.get("/test")

    hsts = response.headers["Strict-Transport-Security"]
    assert "includeSubDomains" not in hsts


def test_custom_hsts_max_age(app):
    """Test custom HSTS max-age value."""
    app.add_middleware(
        SecurityHeadersMiddleware,
        hsts_max_age=63072000,  # 2 years
    )
    client = TestClient(app, base_url="https://testserver")

    response = client.get("/test")

    hsts = response.headers["Strict-Transport-Security"]
    assert "max-age=63072000" in hsts


def test_factory_function(app):
    """Test the create_security_headers_middleware factory function."""
    CustomMiddleware = create_security_headers_middleware(
        frame_options="SAMEORIGIN",
        hsts_max_age=86400,
    )
    app.add_middleware(CustomMiddleware)
    client = TestClient(app)

    response = client.get("/test")

    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"


def test_all_headers_present(client_with_headers):
    """Test that all expected security headers are present."""
    response = client_with_headers.get("/test")

    expected_headers = [
        "X-Content-Type-Options",
        "X-Frame-Options",
        "X-XSS-Protection",
        "Referrer-Policy",
        "Permissions-Policy",
        "Content-Security-Policy",
    ]

    for header in expected_headers:
        assert header in response.headers, f"Missing header: {header}"


def test_headers_not_overridden_if_set_by_endpoint(app):
    """Test that endpoint-set headers are preserved (middleware adds, doesn't override)."""

    @app.get("/custom-header")
    async def custom_header_endpoint():
        from fastapi import Response

        response = Response(content='{"message": "test"}')
        # Note: Middleware runs after endpoint, so it will add headers
        # This test verifies the middleware doesn't break custom headers
        return response

    app.add_middleware(SecurityHeadersMiddleware)
    client = TestClient(app)

    response = client.get("/custom-header")

    # Security headers should still be added
    assert "X-Content-Type-Options" in response.headers


def test_middleware_with_different_request_methods(client_with_headers):
    """Test that security headers are added for all HTTP methods."""
    methods = ["get", "post", "put", "delete", "patch", "head", "options"]

    for method in methods:
        client_method = getattr(client_with_headers, method)
        response = client_method("/test")

        # All methods should have security headers (except possibly 405 for unsupported methods)
        if response.status_code != 405:
            assert "X-Content-Type-Options" in response.headers


@pytest.mark.parametrize(
    "frame_option",
    ["DENY", "SAMEORIGIN", "ALLOW-FROM https://example.com"],
)
def test_various_frame_options(app, frame_option):
    """Test various X-Frame-Options values."""
    app.add_middleware(
        SecurityHeadersMiddleware,
        frame_options=frame_option,
    )
    client = TestClient(app)

    response = client.get("/test")

    assert response.headers["X-Frame-Options"] == frame_option


def test_empty_csp_policy(app):
    """Test that empty CSP policy still uses default."""
    app.add_middleware(
        SecurityHeadersMiddleware,
        csp_policy="",
    )
    client = TestClient(app)

    response = client.get("/test")

    # Empty string is falsy, so default should be used
    assert "Content-Security-Policy" in response.headers
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


def test_none_csp_policy_uses_default(app):
    """Test that None CSP policy uses default."""
    app.add_middleware(
        SecurityHeadersMiddleware,
        csp_policy=None,
    )
    client = TestClient(app)

    response = client.get("/test")

    # Should use default CSP
    assert "Content-Security-Policy" in response.headers
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]


# ---------------------------------------------------------------------------
# Route-set headers win
# ---------------------------------------------------------------------------


def test_route_csp_is_not_replaced_by_the_global_policy(app):
    from fastapi import Response

    @app.get("/download")
    async def download():
        return Response(
            content=b"<svg/>",
            headers={
                "Content-Security-Policy": "default-src 'none'; sandbox",
                "X-Frame-Options": "SAMEORIGIN",
            },
        )

    app.add_middleware(SecurityHeadersMiddleware)
    response = TestClient(app).get("/download")
    assert response.headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    # Headers the route did not set still get the global defaults.
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_download_route_keeps_its_sandbox_csp_behind_the_middleware():
    """The real media download response, served through the middleware."""
    from fastapi import FastAPI

    from tenxgraph_api.src.app.routers.media.router import _download_response

    app = FastAPI()

    @app.get("/file")
    async def file():
        return _download_response("f1", b"<html><script>x</script></html>", "text/html")

    app.add_middleware(SecurityHeadersMiddleware)
    response = TestClient(app).get("/file")
    assert response.headers["Content-Security-Policy"] == "default-src 'none'; sandbox"
    assert response.headers["Content-Disposition"].startswith("attachment")
