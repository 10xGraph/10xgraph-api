from abc import ABC, abstractmethod
from typing import Any

from fastapi import Response
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import HTTPConnection
from tenxgraph.core.authz import Identity

from tenxgraph_api.src.app.core.auth.rbac import RbacConfig


class BaseAuth(ABC):
    # Set True when ``authenticate`` is a pure function of the credential (no response side
    # effects), so a result verified earlier in the same request (by the rate limiter) can be
    # reused instead of verifying the token twice. See ``core/auth/permissions.py``.
    reusable_within_request: bool = False

    # RBAC settings; the loader sets this from the ``auth`` config. None means the default set.
    rbac: RbacConfig | None = None

    @abstractmethod
    def authenticate(
        self,
        request: HTTPConnection,
        response: Response | None,
        credential: HTTPAuthorizationCredentials | None,
    ) -> dict[str, Any] | None:
        """Return the verified token claims; they must include ``user_id``.

        Raise to refuse (an ``HTTPException`` or a ``UserAccountError`` answers 401; a
        WebSocket is closed with 1008). ``response`` is None when the caller has none.
        Example: ``return {"user_id": "12345", "email": "johndoe@example.com", "roles": ["ops"]}``
        """

    async def authorize(
        self, identity: Identity, permission: str, resource: str | None = None
    ) -> bool:
        """Whether ``identity`` may do ``permission`` (``resource:action``) on ``resource``.

        The default is the built-in RBAC: ``identity.can(permission)``. Override it for custom
        rules (a DB lookup, Casbin, Cerbos). It takes no request, so the jobs worker can call it.
        """
        return identity.can(permission)
