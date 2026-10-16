"""
API Key Authentication Backend Example

This example shows how to implement a custom API key authentication backend
for 10xGraph CLI applications.

Use Case:
- Service-to-service authentication
- Third-party API integrations
- Simpler alternative to OAuth2

Setup:
1. Create this file in your project (e.g., auth/api_key.py)
2. Configure 10xgraph.json to use this backend
3. Set API_KEYS environment variable
"""

import hashlib
import hmac
import os
from datetime import datetime
from typing import Any

from fastapi import HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials

# Import the base authentication class
# In your project: from tenxgraph_api import BaseAuth
from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth


def _api_key_pepper() -> bytes:
    """Server-side secret mixed into every API key hash (``API_KEY_PEPPER``).

    With HMAC, a leaked table of hashes cannot be checked offline without this secret.
    """
    pepper = os.getenv("API_KEY_PEPPER", "")
    if not pepper:
        raise RuntimeError("Set API_KEY_PEPPER to a long random secret to hash API keys.")
    return pepper.encode()


class APIKeyAuth(BaseAuth):
    """
    API Key authentication backend.

    Validates API keys from Authorization header and returns user context.
    Supports both plain keys and hashed keys for better security.
    """

    def __init__(self):
        """Initialize API key authentication."""
        # Load API keys from environment
        # Format: key1,key2,key3 or key1:user1,key2:user2
        keys_string = os.getenv("API_KEYS", "")

        if not keys_string:
            print("WARNING: No API keys configured. Set API_KEYS environment variable.")

        # Parse API keys and associated metadata
        self.valid_keys: dict[str, dict[str, Any]] = {}

        for key_entry in keys_string.split(","):
            if ":" in key_entry:
                # Format: key:user_id:role
                parts = key_entry.strip().split(":")
                key = parts[0]
                user_id = parts[1] if len(parts) > 1 else f"api_key_{key[:8]}"
                role = parts[2] if len(parts) > 2 else "user"

                self.valid_keys[key] = {"user_id": user_id, "role": role, "key_prefix": key[:8]}
            else:
                # Format: key (use prefix as user_id)
                key = key_entry.strip()
                if key:
                    self.valid_keys[key] = {
                        "user_id": f"api_key_{key[:8]}",
                        "role": "user",
                        "key_prefix": key[:8],
                    }

        print(f"Loaded {len(self.valid_keys)} API keys")

    async def authenticate(
        self, credentials: HTTPAuthorizationCredentials, response: Response
    ) -> dict[str, str]:
        """
        Authenticate request using API key.

        Args:
            credentials: HTTP authorization credentials (Bearer token)
            response: FastAPI response object

        Returns:
            User context dictionary with user_id and metadata

        Raises:
            HTTPException: If API key is invalid or missing
        """
        if not credentials:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key required",
                headers={"WWW-Authenticate": "Bearer"},
            )

        api_key = credentials.credentials

        # Validate API key exists and is not empty
        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key cannot be empty",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Check if API key is valid
        key_metadata = self.valid_keys.get(api_key)

        if not key_metadata:
            # Log the failed attempt without any part of the key
            print("Invalid API key attempt")

            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Log successful authentication
        print(f"API key authenticated: {key_metadata['user_id']}")

        # Return user context
        return {
            "user_id": key_metadata["user_id"],
            "auth_method": "api_key",
            "role": key_metadata["role"],
            "key_prefix": key_metadata["key_prefix"],
            "authenticated_at": datetime.utcnow().isoformat(),
        }

    def extract_user_id(self, user: dict[str, str]) -> str | None:
        """
        Extract user ID from user context.

        Args:
            user: User context dictionary

        Returns:
            User ID string or None
        """
        return user.get("user_id")


class HashedAPIKeyAuth(BaseAuth):
    """
    Hashed API Key authentication - more secure variant.

    Stores hashed versions of API keys instead of plain text.
    Recommended for production use.
    """

    def __init__(self):
        """Initialize hashed API key authentication."""
        # Load hashed API keys from environment
        # Format: hashed_key1:user1,hashed_key2:user2
        keys_string = os.getenv("API_KEY_HASHES", "")

        self.valid_key_hashes: dict[str, dict[str, Any]] = {}

        for key_entry in keys_string.split(","):
            if ":" in key_entry:
                parts = key_entry.strip().split(":")
                key_hash = parts[0]
                user_id = parts[1] if len(parts) > 1 else f"user_{key_hash[:8]}"
                role = parts[2] if len(parts) > 2 else "user"

                self.valid_key_hashes[key_hash] = {"user_id": user_id, "role": role}

        print(f"Loaded {len(self.valid_key_hashes)} hashed API keys")

    def hash_key(self, api_key: str) -> str:
        """
        Hash an API key with HMAC-SHA-256 and the server-side pepper.

        Args:
            api_key: Plain text API key

        Returns:
            Hexadecimal hash of the API key
        """
        return hmac.new(_api_key_pepper(), api_key.encode(), hashlib.sha256).hexdigest()

    async def authenticate(
        self, credentials: HTTPAuthorizationCredentials, response: Response
    ) -> dict[str, str]:
        """Authenticate using hashed API key."""
        if not credentials:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key required",
                headers={"WWW-Authenticate": "Bearer"},
            )

        api_key = credentials.credentials
        key_hash = self.hash_key(api_key)

        # Check if hash matches
        key_metadata = self.valid_key_hashes.get(key_hash)

        if not key_metadata:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

        return {
            "user_id": key_metadata["user_id"],
            "auth_method": "hashed_api_key",
            "role": key_metadata["role"],
            "authenticated_at": datetime.utcnow().isoformat(),
        }

    def extract_user_id(self, user: dict[str, str]) -> str | None:
        """Extract user ID from user context."""
        return user.get("user_id")


# Helper function to generate API keys
def generate_api_key(prefix: str = "ak", length: int = 32) -> str:
    """
    Generate a secure random API key.

    Args:
        prefix: Key prefix for identification
        length: Length of random portion

    Returns:
        Generated API key
    """
    import secrets

    random_part = secrets.token_urlsafe(length)
    return f"{prefix}_{random_part}"


# Helper function to hash existing keys
def hash_api_key(api_key: str) -> str:
    """
    Hash an API key for storage.

    Args:
        api_key: Plain text API key

    Returns:
        HMAC-SHA-256 of the key (hex)
    """
    return hmac.new(_api_key_pepper(), api_key.encode(), hashlib.sha256).hexdigest()


"""
CONFIGURATION:

# 10xgraph.json
{
  "auth": {
    "method": "custom",
    "path": "auth.api_key:APIKeyAuth"
  },
  "agent": "graph.react:app"
}

# .env - Plain text keys (development only)
API_KEYS=ak_dev123:user_dev:developer,ak_admin456:user_admin:admin

# .env - Hashed keys (production); hashes depend on the pepper, so keep it stable
API_KEY_PEPPER=<long random secret>
API_KEY_HASHES=<hash1>:user1:admin,<hash2>:user2:developer
"""

"""
USAGE EXAMPLE:

# Generate a new API key
python -c "from api_key_auth import generate_api_key; print(generate_api_key())"
# Output: ak_xK9mP2nQ7vL3wR8dF5hJ1bN4cT6yU0zA

# Hash an existing key for production
python -c "from api_key_auth import hash_api_key; print(hash_api_key('ak_dev123'))"
# Output: 5d41402abc4b2a76b9719d911017c592...

# Use API key in requests
curl http://localhost:8000/graph/invoke \
  -H "Authorization: Bearer ak_dev123" \
  -H "Content-Type: application/json" \
  -d '{"input": {"message": "hello"}}'
"""

"""
SECURITY BEST PRACTICES:

1. Key Generation:
   - Use cryptographically secure random generation
   - Minimum 32 characters
   - Include prefix for identification

2. Key Storage:
   - Store hashed keys in production
   - Use environment variables or secret manager
   - Never commit keys to version control

3. Key Rotation:
   - Rotate keys regularly (e.g., every 90 days)
   - Support multiple active keys during rotation
   - Revoke compromised keys immediately

4. Usage:
   - Use HTTPS only
   - Implement rate limiting
   - Log all authentication attempts
   - Monitor for suspicious patterns

5. Access Control:
   - Associate keys with specific users/services
   - Implement key-level permissions
   - Separate keys for different environments
"""

"""
TESTING:

# tests/test_api_key_auth.py
import pytest
from fastapi.testclient import TestClient
from auth.api_key import APIKeyAuth, generate_api_key

def test_valid_api_key():
    # Test with valid key
    pass

def test_invalid_api_key():
    # Test with invalid key
    pass

def test_missing_api_key():
    # Test without Authorization header
    pass

def test_key_generation():
    key = generate_api_key()
    assert key.startswith("ak_")
    assert len(key) > 32
"""
