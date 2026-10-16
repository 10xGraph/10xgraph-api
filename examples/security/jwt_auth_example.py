"""
JWT Authentication Example

This example demonstrates how to use the built-in JWT authentication
in 10xGraph CLI applications.

Setup:
1. Configure 10xgraph.json with "auth": "jwt"
2. Set JWT_SECRET_KEY environment variable
3. Run the application

Usage:
1. Generate JWT token with your authentication service
2. Include token in Authorization header
3. Access protected endpoints
"""

import os
from datetime import datetime, timedelta
from typing import Any

import jwt
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel


# Example configuration
JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "dev-secret-key-change-in-production")
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
JWT_ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_ACCESS_TOKEN_EXPIRE_MINUTES", "30"))

# Security scheme
security = HTTPBearer()


class TokenData(BaseModel):
    """JWT token payload structure."""

    user_id: str
    email: str
    role: str = "user"
    exp: datetime | None = None


class LoginRequest(BaseModel):
    """Login request structure."""

    username: str
    password: str


class TokenResponse(BaseModel):
    """Token response structure."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int


def create_access_token(data: dict[str, Any], expires_delta: timedelta | None = None) -> str:
    """
    Create a JWT access token.

    Args:
        data: Token payload data
        expires_delta: Token expiration time

    Returns:
        Encoded JWT token string
    """
    to_encode = data.copy()

    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=JWT_ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode.update({"exp": expire})

    encoded_jwt = jwt.encode(to_encode, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)
    return encoded_jwt


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict[str, Any]:
    """
    Verify JWT token and extract user data.

    Args:
        credentials: HTTP authorization credentials

    Returns:
        User data from token

    Raises:
        HTTPException: If token is invalid or expired
    """
    token = credentials.credentials

    try:
        payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
        user_id = payload.get("user_id")

        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return payload

    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


# Example FastAPI application
app = FastAPI(title="JWT Auth Example")


@app.post("/auth/login", response_model=TokenResponse)
async def login(request: LoginRequest):
    """
    Login endpoint - generates JWT token.

    In production:
    - Validate credentials against database
    - Hash password comparison
    - Rate limiting
    - Account lockout after failed attempts
    """
    # Example: Hardcoded user (replace with database lookup)
    if request.username == "admin" and request.password == "secret":
        # Create token data
        token_data = {"user_id": "user_123456789", "email": "admin@example.com", "role": "admin"}

        # Generate token
        access_token = create_access_token(token_data)

        return TokenResponse(
            access_token=access_token, expires_in=JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Incorrect username or password"
    )


@app.get("/auth/me")
async def get_current_user(user: dict = Depends(verify_token)):
    """
    Get current user information from token.

    Protected endpoint - requires valid JWT token.
    """
    return {"user_id": user.get("user_id"), "email": user.get("email"), "role": user.get("role")}


@app.get("/protected")
async def protected_endpoint(user: dict = Depends(verify_token)):
    """
    Example protected endpoint.

    This endpoint requires a valid JWT token in the Authorization header.
    """
    return {
        "message": f"Hello {user.get('email')}!",
        "user_id": user.get("user_id"),
        "timestamp": datetime.utcnow().isoformat(),
    }


# Example usage
if __name__ == "__main__":
    import uvicorn

    print("Starting JWT Auth Example Server")
    print(f"Secret Key: {'set' if JWT_SECRET_KEY else 'MISSING'}")
    print(f"Algorithm: {JWT_ALGORITHM}")
    print(f"Token Expiration: {JWT_ACCESS_TOKEN_EXPIRE_MINUTES} minutes")
    print("\nTest the API:")
    print("1. Login: POST http://localhost:8000/auth/login")
    print('   Body: {"username": "admin", "password": "secret"}')
    print("2. Access: GET http://localhost:8000/protected")
    print("   Header: Authorization: Bearer <token>")

    uvicorn.run(app, host="0.0.0.0", port=8000)


"""
TESTING EXAMPLE:

# 1. Start the server
python jwt_auth_example.py

# 2. Login to get token
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "secret"}'

# Response:
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "token_type": "bearer",
  "expires_in": 1800
}

# 3. Use token to access protected endpoint
export TOKEN="<access_token_from_above>"
curl http://localhost:8000/protected \
  -H "Authorization: Bearer $TOKEN"

# Response:
{
  "message": "Hello admin@example.com!",
  "user_id": "user_123456789",
  "timestamp": "2025-12-31T12:00:00"
}

# 4. Get current user info
curl http://localhost:8000/auth/me \
  -H "Authorization: Bearer $TOKEN"
"""

"""
PRODUCTION CONFIGURATION:

# .env
JWT_SECRET_KEY=<generate-with-secrets.token_urlsafe(32)>
JWT_ALGORITHM=HS256
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=30

# 10xgraph.json
{
  "auth": "jwt",
  "agent": "graph.react:app"
}

# Security Recommendations:
1. Generate strong secret: python -c "import secrets; print(secrets.token_urlsafe(32))"
2. Use environment variables for secrets (never hardcode)
3. Set short token expiration (15-30 minutes)
4. Implement refresh token mechanism for long sessions
5. Use HTTPS in production
6. Implement rate limiting on login endpoint
7. Add account lockout after failed attempts
8. Log authentication events
9. Consider token blacklist for logout
10. Rotate secrets regularly
"""
