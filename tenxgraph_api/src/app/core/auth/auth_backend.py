from typing import Any

from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from injectq.integrations import InjectAPI

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.auth.base_auth import BaseAuth
from tenxgraph_api.src.app.core.auth.reserved_identity import (
    RESERVED_USER_ID_MESSAGE,
    identity_is_reserved,
    log_reserved_rejection,
)
from tenxgraph_api.src.app.core.config.graph_config import GraphConfig


def verify_current_user(
    request: Request,
    response: Response,
    credential: HTTPAuthorizationCredentials = Depends(
        HTTPBearer(auto_error=False),
    ),
    config: GraphConfig = InjectAPI(GraphConfig),
    auth_backend: BaseAuth = InjectAPI(BaseAuth),
) -> dict[str, Any]:
    # check auth backend
    user = {}
    backend = config.auth_config()
    if not backend:
        return user

    if not auth_backend:
        logger.error("Auth backend is not configured")
        return user

    user: dict | None = auth_backend.authenticate(
        request,
        response,
        credential,
    )
    if identity_is_reserved(user):
        log_reserved_rejection()
        raise HTTPException(status_code=401, detail=RESERVED_USER_ID_MESSAGE)
    if user and "user_id" not in user:
        logger.error("Authentication failed: 'user_id' not found in user info")
    return user or {}
