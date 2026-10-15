from abc import ABC, abstractmethod
from typing import Any

from fastapi import Request, Response
from fastapi.security import HTTPAuthorizationCredentials


class BaseAuth(ABC):
    # Set True when ``authenticate`` is a pure function of the credential (no response side
    # effects), so a result verified earlier in the same request (by the rate limiter) can be
    # reused instead of verifying the token twice. See ``core/auth/identity_cache.py``.
    reusable_within_request: bool = False

    @abstractmethod
    def authenticate(
        self,
        request: Request,
        response: Response,
        credential: HTTPAuthorizationCredentials,
    ) -> dict[str, Any] | None:
        """Authenticate the user based on the provided credentials.
        IT should return an empty dict if no authentication is required.
        If authentication fails, it should raise an appropriate exception.
        In case authentication is successful, it should return a dict with user information,
        containing at least a 'user_id' key, and optionally other user details.
        Example:
            return {"user_id": "12345", "username": "johndoe", "email": "johndoe@example.com"}

        What ever keys are returned that will be merged with config in the main graph app.
        """

        raise NotImplementedError
