import time
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

# Signature algorithms JWT_ALGORITHM may name. ``none`` and anything not listed is refused.
ALLOWED_JWT_ALGORITHMS: frozenset[str] = frozenset(
    {
        "HS256",
        "HS384",
        "HS512",
        "RS256",
        "RS384",
        "RS512",
        "ES256",
        "ES384",
        "ES512",
        "PS256",
        "PS384",
        "PS512",
        "EdDSA",
    }
)


def _require_pyjwt() -> None:
    if jwt is None:
        raise ImportError(
            "PyJWT is required for JWT authentication. "
            'Install with `pip install "10xgraph-api[jwt]"`'
        )


def check_jwt_settings(settings: Settings) -> None:
    """Check the JWT settings at startup.

    Refuses a missing PyJWT and an algorithm outside ``ALLOWED_JWT_ALGORITHMS`` (``none``
    included), always. Refuses a guessable HMAC secret in production, warns about it otherwise.

    A short HS* secret can be brute-forced offline from any one token, after which anyone can
    mint a token for any ``user_id``.
    """
    _require_pyjwt()
    configured = settings.JWT_ALGORITHM or ""
    if configured not in ALLOWED_JWT_ALGORITHMS:
        raise ValueError(
            f"JWT_ALGORITHM {configured!r} is not allowed. Use one of: "
            f"{', '.join(sorted(ALLOWED_JWT_ALGORITHMS))} (HS256 is the default)."
        )
    algorithm = configured.upper()
    if not algorithm.startswith("HS"):
        return
    secrets = [key for _, key in verification_keys(settings)]
    if all(len(secret.encode()) >= MIN_HMAC_BYTES for secret in secrets):
        return
    message = (
        f"A JWT secret key is shorter than {MIN_HMAC_BYTES} bytes, which is too weak for "
        f"{algorithm}. Generate one with: "
        "python -c 'import secrets; print(secrets.token_urlsafe(48))'"
    )
    if settings.MODE == "production":
        raise ValueError(message)
    logger.warning(message)


def verification_keys(settings: Settings) -> list[tuple[str | None, str]]:
    """``(kid, key)`` for every configured verification key: ``JWT_SECRET_KEY`` first (no kid),
    then each entry of ``JWT_SECRET_KEYS``."""
    keys: list[tuple[str | None, str]] = []
    if settings.JWT_SECRET_KEY:
        keys.append((None, settings.JWT_SECRET_KEY))
    keys.extend((entry.kid, entry.key) for entry in settings.JWT_SECRET_KEYS or [])
    return keys


def _candidate_keys(token: str, keys: list[tuple[str | None, str]]) -> list[str]:
    """The keys to try for ``token``: those matching its header ``kid``, else the kid-less ones.

    A header ``kid`` that names a configured key selects only the keys with that ``kid``. A
    token without ``kid`` (or with one nobody configured) is tried against every key that has
    no ``kid`` of its own, then the rest, so ``JWT_SECRET_KEY`` keeps verifying as before.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError:
        return [key for _, key in keys]
    kid = header.get("kid")
    if isinstance(kid, str) and kid:
        matched = [key for key_id, key in keys if key_id == kid]
        if matched:
            return matched
        return [key for key_id, key in keys if key_id is None]
    return [key for _, key in keys]


def _decode_with_keys(
    token: str,
    keys: list[tuple[str | None, str]],
    algorithm: str,
    settings: Settings,
    required: list[str],
) -> dict[str, Any]:
    """Verify ``token`` against the candidate keys; the first key that verifies wins."""
    failure: Exception | None = None
    for candidate in _candidate_keys(token, keys):
        try:
            return jwt.decode(
                token,
                candidate,
                algorithms=[algorithm],
                issuer=settings.JWT_ISSUER or None,
                audience=settings.JWT_AUDIENCE or None,
                options={"require": required},
            )
        except jwt.InvalidSignatureError as err:
            failure = failure or err  # another key may verify it
        except jwt.ExpiredSignatureError:
            raise _unauthorized("Token has expired, please login again", "EXPIRED_TOKEN")
        except jwt.InvalidTokenError as err:
            failure = err
            break
    logger.warning("JWT rejected: %s", type(failure).__name__)
    raise _unauthorized("Invalid token, please login again", "INVALID_TOKEN")


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
        keys = verification_keys(settings)
        jwt_algorithm = settings.JWT_ALGORITHM

        token = credential.credentials

        _require_pyjwt()

        if not keys or jwt_algorithm is None:
            raise UserAccountError(
                message="JWT settings are not configured",
                error_code="JWT_SETTINGS_NOT_CONFIGURED",
                status_code=500,
            )

        if jwt_algorithm not in ALLOWED_JWT_ALGORITHMS:
            raise UserAccountError(
                message="JWT settings are not configured",
                error_code="JWT_SETTINGS_NOT_CONFIGURED",
                status_code=500,
            )

        required = ["exp"]
        if settings.JWT_MAX_TOKEN_AGE_SECONDS:
            required.append("iat")
        if settings.JWT_ISSUER:
            required.append("iss")
        if settings.JWT_AUDIENCE:
            required.append("aud")
        decoded_token = _decode_with_keys(token, keys, jwt_algorithm, settings, required)
        max_age = settings.JWT_MAX_TOKEN_AGE_SECONDS
        if max_age:
            issued_at = decoded_token.get("iat")
            if (
                isinstance(issued_at, bool)
                or not isinstance(issued_at, int | float)
                or time.time() - issued_at > max_age
            ):
                raise _unauthorized("Token is too old, please login again", "EXPIRED_TOKEN")

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
