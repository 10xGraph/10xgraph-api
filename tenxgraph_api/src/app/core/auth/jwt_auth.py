from typing import Any

from fastapi import Request, Response
from fastapi.security import HTTPAuthorizationCredentials

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.auth.authorization import ScopeClaimError, scopes_from_claims
from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
from tenxgraph_api.src.app.core.config.settings import Settings, get_settings
from tenxgraph_api.src.app.core.exceptions import UserAccountError


try:
    import jwt
except ImportError:  # pragma: no cover
    jwt = None  # type: ignore[assignment]


# RFC 7518 3.2: an HMAC key must be at least as long as the hash output (256 bits for HS256).
MIN_HMAC_BYTES = 32


def check_jwt_settings(settings: Settings) -> None:
    """Refuse a guessable HMAC secret at startup (production), warn about it otherwise.

    A short HS* secret can be brute-forced offline from any one token, after which anyone can
    mint a token for any ``user_id``.
    """
    algorithm = (settings.JWT_ALGORITHM or "").upper()
    secret = settings.JWT_SECRET_KEY or ""
    if not algorithm.startswith("HS") or len(secret.encode()) >= MIN_HMAC_BYTES:
        return
    message = (
        f"JWT_SECRET_KEY is shorter than {MIN_HMAC_BYTES} bytes, which is too weak for "
        f"{algorithm}. Generate one with: "
        "python -c 'import secrets; print(secrets.token_urlsafe(48))'"
    )
    if settings.MODE == "production":
        raise ValueError(message)
    logger.warning(message)


def _unauthorized(message: str, error_code: str) -> UserAccountError:
    return UserAccountError(message=message, error_code=error_code, status_code=401)


def _normalise_user_id(value: Any) -> str:
    """A non-empty string, or a whole number (not a bool) as its string form; else 401."""
    if isinstance(value, bool):
        raise _unauthorized("Invalid token, user_id is malformed", "INVALID_TOKEN")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip():
        return value
    raise _unauthorized("Invalid token, user_id is malformed", "INVALID_TOKEN")


class JwtAuth(BaseAuth):
    # Decoding is a pure function of the token, so the rate limiter's result can be reused.
    reusable_within_request = True

    def authenticate(
        self,
        request: Request,
        response: Response,
        credential: HTTPAuthorizationCredentials,
    ) -> dict[str, Any] | None:
        """No authentication is required, so return None."""
        """
        Get the current user based on the provided HTTP
        Authorization credentials.

        Args:
            res (Response): The response object to set headers if needed.
            credential (HTTPAuthorizationCredentials): The HTTP Authorization
            credentials obtained from the request.

        Returns:
            UserSchema: A UserSchema object containing the decoded user information.

        Raises:
            HTTPException: If the credentials are missing.
            UserAccountError: If there are token verification errors such as
                RevokedIdTokenError,
                UserDisabledError,
                InvalidIdTokenError,
                or any other unexpected exceptions.
        """

        if credential is None:
            raise _unauthorized("Invalid token, please login again", "REVOKED_TOKEN")

        settings = get_settings()
        jwt_secret_key = settings.JWT_SECRET_KEY
        jwt_algorithm = settings.JWT_ALGORITHM

        token = credential.credentials

        if jwt is None:
            raise ImportError(
                "PyJWT is required for JWT authentication. "
                'Install with `pip install "10xgraph-api[jwt]"`'
            )

        if jwt_secret_key is None or jwt_algorithm is None:
            raise UserAccountError(
                message="JWT settings are not configured",
                error_code="JWT_SETTINGS_NOT_CONFIGURED",
                status_code=500,
            )

        required = ["exp"]
        if settings.JWT_ISSUER:
            required.append("iss")
        if settings.JWT_AUDIENCE:
            required.append("aud")
        try:
            decoded_token = jwt.decode(
                token,
                jwt_secret_key,
                algorithms=[jwt_algorithm],
                issuer=settings.JWT_ISSUER or None,
                audience=settings.JWT_AUDIENCE or None,
                options={"require": required},
            )
        except jwt.ExpiredSignatureError:
            raise _unauthorized("Token has expired, please login again", "EXPIRED_TOKEN")
        except jwt.InvalidTokenError as err:
            logger.warning("JWT rejected: %s", type(err).__name__)
            raise _unauthorized("Invalid token, please login again", "INVALID_TOKEN")

        response.headers["WWW-Authenticate"] = 'Bearer realm="auth_required"'

        # check if user_id exists in the token
        if "user_id" not in decoded_token:
            raise _unauthorized("Invalid token, user_id missing", "INVALID_TOKEN")
        decoded_token["user_id"] = _normalise_user_id(decoded_token["user_id"])
        try:
            scopes_from_claims(decoded_token)
        except ScopeClaimError:
            raise _unauthorized("Invalid token, scope claim is malformed", "INVALID_TOKEN")
        return decoded_token
