# Production Configuration Setup Guide

This directory contains production-ready configuration examples for deploying 10xGraph CLI applications securely.

## Files Overview

- **10xgraph.json** - Production application configuration with JWT auth and RBAC
- **.env.production.example** - Complete environment variables template
- **docker-compose.yml** - Production Docker deployment with security hardening
- **nginx.conf** - Nginx reverse proxy with SSL/TLS and security headers
- **README.md** - This file

## Quick Start

### 1. Copy Configuration Files

```bash
# Copy to your project root
cp 10xgraph.json /path/to/your/project/
cp .env.production.example /path/to/your/project/.env.production
cp docker-compose.yml /path/to/your/project/
cp nginx.conf /path/to/your/project/
```

### 2. Configure Environment Variables

```bash
# Edit .env.production
nano .env.production

# Generate JWT secret
python -c "import secrets; print(secrets.token_urlsafe(32))"

# Set the generated key in .env.production
JWT_SECRET_KEY=<generated-key>
```

### 3. Update Domain Names

Update the following files with your domain:

**docker-compose.yml:**
```yaml
environment:
  - ORIGINS=https://yourdomain.com
  - ALLOWED_HOST=yourdomain.com
```

**nginx.conf:**
```nginx
server_name yourdomain.com www.yourdomain.com;
```

### 4. SSL/TLS Certificates

#### Option A: Let's Encrypt (Recommended)

```bash
# Install certbot
sudo apt-get install certbot python3-certbot-nginx

# Get certificate
sudo certbot --nginx -d yourdomain.com -d www.yourdomain.com

# Certificates will be in: /etc/letsencrypt/live/yourdomain.com/
```

#### Option B: Self-Signed (Development Only)

```bash
# Create SSL directory
mkdir -p ssl

# Generate self-signed certificate
openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout ssl/key.pem \
  -out ssl/cert.pem \
  -subj "/CN=yourdomain.com"
```

Update nginx.conf paths:
```nginx
ssl_certificate /etc/nginx/ssl/cert.pem;
ssl_certificate_key /etc/nginx/ssl/key.pem;
```

### 5. Create Authorization Backend

Create your RBAC backend:

```bash
# Create auth directory
mkdir -p auth

# Copy RBAC example
cp ../rbac_authorization.py auth/rbac_backend.py
```

Or implement custom authorization (see examples).

### 6. Deploy

```bash
# Build and start services
docker-compose up -d --build

# Check logs
docker-compose logs -f api

# Verify health
curl https://yourdomain.com/ping
```

## Security Checklist

Before going to production, verify:

### Required Configuration
- [ ] `MODE=production` in .env.production
- [ ] Strong `JWT_SECRET_KEY` (32+ chars, random)
- [ ] Specific `ORIGINS` (not `*`)
- [ ] Specific `ALLOWED_HOST` (not `*`)
- [ ] `DOCS_PATH` and `REDOCS_PATH` are empty
- [ ] Valid SSL/TLS certificates installed

### Network Security
- [ ] HTTPS enabled and working
- [ ] HTTP redirects to HTTPS
- [ ] Firewall rules configured
- [ ] Rate limiting active
- [ ] Security headers present

### Application Security
- [ ] JWT authentication enabled
- [ ] Authorization backend implemented
- [ ] Request size limits configured
- [ ] Error messages sanitized
- [ ] Logs sanitized

### Infrastructure
- [ ] Services run as non-root users
- [ ] Read-only filesystems where possible
- [ ] Resource limits configured
- [ ] Health checks working
- [ ] Logging configured
- [ ] Backups scheduled

## Testing Production Configuration

### 1. Test HTTP to HTTPS Redirect

```bash
curl -I http://yourdomain.com
# Should return 301 redirect to https://
```

### 2. Test Security Headers

```bash
curl -I https://yourdomain.com
# Should include:
# Strict-Transport-Security
# X-Content-Type-Options
# X-Frame-Options
# Content-Security-Policy
```

### 3. Test Authentication

```bash
# Should fail without token
curl https://yourdomain.com/graph/invoke
# Response: 401 Unauthorized

# Should succeed with valid token
curl -H "Authorization: Bearer <token>" \
  https://yourdomain.com/graph/invoke
```

### 4. Test Rate Limiting

```bash
# Rapid requests should trigger rate limit
for i in {1..20}; do 
  curl https://yourdomain.com/ping
done
# Should eventually return 429 Too Many Requests
```

### 5. Test Request Size Limit

```bash
# Large request should be rejected
dd if=/dev/zero bs=1M count=15 | \
  curl -X POST https://yourdomain.com/graph/invoke \
    -H "Authorization: Bearer <token>" \
    -H "Content-Type: application/json" \
    --data-binary @-
# Should return 413 Request Entity Too Large
```

## Monitoring

### View Logs

```bash
# API logs
docker-compose logs -f api

# Nginx logs
docker-compose logs -f nginx

# Redis logs
docker-compose logs -f redis

# All logs
docker-compose logs -f
```

### Check Service Health

```bash
# All services
docker-compose ps

# API health
curl https://yourdomain.com/ping

# Redis health
docker-compose exec redis redis-cli ping
```

### Resource Usage

```bash
# Container stats
docker stats

# Disk usage
docker system df
```

## Backup and Recovery

### Backup Redis Data

```bash
# Create backup
docker-compose exec redis redis-cli BGSAVE

# Copy backup file
docker cp $(docker-compose ps -q redis):/data/dump.rdb ./backup/
```

### Restore Redis Data

```bash
# Stop services
docker-compose down

# Restore backup
docker cp ./backup/dump.rdb $(docker-compose ps -q redis):/data/

# Start services
docker-compose up -d
```

## Scaling

### Horizontal Scaling

Add multiple API instances:

**docker-compose.yml:**
```yaml
api:
  deploy:
    replicas: 3
```

Update nginx upstream:
```nginx
upstream api_backend {
    least_conn;
    server api:8000;
    server api2:8000;
    server api3:8000;
}
```

### Vertical Scaling

Adjust resource limits:

```yaml
deploy:
  resources:
    limits:
      cpus: '4'
      memory: 4G
    reservations:
      cpus: '2'
      memory: 1G
```

## Troubleshooting

### Issue: Services won't start

```bash
# Check logs
docker-compose logs

# Check configuration
docker-compose config

# Validate environment
docker-compose exec api env
```

### Issue: SSL certificate errors

```bash
# Verify certificates
openssl x509 -in ssl/cert.pem -text -noout

# Test SSL configuration
openssl s_client -connect yourdomain.com:443
```

### Issue: Rate limiting too strict

Adjust nginx.conf:
```nginx
# Increase rate
limit_req_zone $binary_remote_addr zone=api:10m rate=20r/s;

# Increase burst
limit_req zone=api burst=50 nodelay;
```

### Issue: High memory usage

```bash
# Check memory usage
docker stats

# Adjust limits
# Edit docker-compose.yml resources section
```

## Security Maintenance

### Regular Tasks

**Daily:**
- Monitor logs for suspicious activity
- Check service health

**Weekly:**
- Review access logs
- Update rate limiting rules if needed
- Check resource usage trends

**Monthly:**
- Rotate JWT secrets
- Update dependencies
- Review authorization rules
- Security audit

**Quarterly:**
- SSL certificate renewal (if not auto-renewed)
- Penetration testing
- Security policy review

### Update Dependencies

```bash
# Pull latest images
docker-compose pull

# Rebuild with latest base images
docker-compose build --no-cache

# Restart services
docker-compose up -d
```

## Additional Resources

- [SECURITY.md](../../../SECURITY.md) - Complete security guide
- [Deployment Guide](../../../docs/deployment.md) - Detailed deployment documentation
- [Configuration Guide](../../../docs/configuration.md) - All configuration options

## Support

For issues or questions:
- GitHub Issues: https://github.com/10xGraph/10xgraph-api/issues
- Email: security@10xhub.com
- Documentation: https://10xhub.com/docs
