# Security Examples

This directory contains comprehensive examples for implementing security features in 10xGraph CLI applications.

## Examples Overview

### Authentication Examples
1. **[jwt_auth_example.py](./jwt_auth_example.py)** - Built-in JWT authentication setup
2. **[api_key_auth.py](./api_key_auth.py)** - Custom API key authentication backend
3. **[oauth2_auth.py](./oauth2_auth.py)** - OAuth2 authentication with external providers

### Authorization Examples
4. **[rbac_authorization.py](./rbac_authorization.py)** - Role-Based Access Control (RBAC)
5. **[ownership_authorization.py](./ownership_authorization.py)** - Resource ownership-based authorization
6. **[abac_authorization.py](./abac_authorization.py)** - Attribute-Based Access Control (ABAC)

### Configuration Examples
7. **[production_config/](./production_config/)** - Secure production configuration templates
   - 10xgraph.json - Production configuration
   - .env.production - Environment variables
   - docker-compose.yml - Docker deployment
   - nginx.conf - Nginx reverse proxy with security headers

## Quick Start

### 1. JWT Authentication

**Step 1:** Configure 10xgraph.json
```json
{
  "auth": "jwt",
  "agent": "graph.react:app"
}
```

**Step 2:** Set environment variables
```bash
export JWT_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(32))")
export JWT_ALGORITHM=HS256
export JWT_ACCESS_TOKEN_EXPIRE_MINUTES=30
```

**Step 3:** Start the application
```bash
10xgraph dev --no-open
```

### 2. Custom Authentication

**Step 1:** Create auth backend (see [api_key_auth.py](./api_key_auth.py))

**Step 2:** Configure 10xgraph.json
```json
{
  "auth": {
    "method": "custom",
    "path": "auth.api_key:APIKeyAuth"
  }
}
```

### 3. Authorization

**Step 1:** Create authorization backend (see [rbac_authorization.py](./rbac_authorization.py))

**Step 2:** Configure 10xgraph.json
```json
{
  "auth": "jwt",
  "authorization": {
    "path": "auth.rbac_backend:RBACAuthorizationBackend"
  }
}
```

## Testing Examples

Each example includes test cases. Run them with:

```bash
# Install test dependencies
pip install pytest pytest-asyncio httpx

# Run tests
pytest examples/security/ -v
```

## Production Deployment

See the [production_config](./production_config/) directory for:
- Complete production configuration
- Docker deployment setup
- Nginx configuration with security headers
- Kubernetes manifests (coming soon)

## Security Best Practices

1. **Never commit secrets** - Use environment variables or secret managers
2. **Use HTTPS in production** - Always encrypt traffic
3. **Implement rate limiting** - Prevent brute force and DoS attacks
4. **Monitor security events** - Log authentication/authorization failures
5. **Regular updates** - Keep dependencies up to date
6. **Security testing** - Include security tests in CI/CD pipeline

## Additional Resources

- [SECURITY.md](../../SECURITY.md) - Complete security guide
- [Authentication Guide](../../docs/authentication.md) - Detailed authentication documentation
- [Deployment Guide](../../docs/deployment.md) - Production deployment guide

## Questions?

For questions or issues, please:
- Check the [SECURITY.md](../../SECURITY.md) guide
- Open an issue on GitHub
- Email: security@10xhub.com
