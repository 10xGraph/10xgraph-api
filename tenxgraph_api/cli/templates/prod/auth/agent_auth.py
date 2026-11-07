import logging
from typing import Any

from fastapi import HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials
from tenxgraph.core.authz import Identity
from tenxgraph_api import BaseAuth


logger = logging.getLogger(__name__)


class AgentAuth(BaseAuth):
    """Verify the caller and return who they are.

    Every request is denied with 401 until ``authenticate`` checks a real credential.

    Contract:
    - Return a dict with at least ``user_id``. It becomes the thread and memory owner, so it
      must come from the verified credential, never from a header or body field the client
      sets. Optional ``roles`` / ``scopes`` feed the authorization backend.
    - Raise ``HTTPException(status_code=401)`` for a missing, invalid or expired credential.
    """

    def __init__(self) -> None:
        logger.warning(
            "AgentAuth.authenticate is not implemented yet: every request will get 401. "
            "Implement it in auth/agent_auth.py."
        )

    def authenticate(
        self,
        request: Request,
        response: Response,
        credential: HTTPAuthorizationCredentials,
    ) -> dict[str, Any] | None:
        if credential is None:
            raise HTTPException(status_code=401, detail="Missing credentials")

        # Replace this with a real check, for example verifying the bearer token with your
        # identity provider:
        #
        #     claims = verify_token(credential.credentials)  # raises on a bad token
        #     return {"user_id": claims["sub"], "roles": claims.get("roles", [])}
        #
        # Remove the warning in __init__ once this is done.
        raise HTTPException(status_code=401, detail="Authentication is not configured")

    async def authorize(
        self, identity: Identity, permission: str, resource: str | None = None
    ) -> bool:
        """Optional. Delete this method to use the built-in role-based checks.

        Example: let the built-in checks decide, then add a rule of your own, such as a lookup
        in your database. ``resource`` is the thread id (or other id) the call is about.
        """
        if not await super().authorize(identity, permission, resource):
            return False
        # if permission == "threads:delete":
        #     return await my_db.is_thread_owner_or_admin(identity.user_id, resource)
        return True
