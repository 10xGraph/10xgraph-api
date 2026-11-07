"""``GET /v1/me``: who the caller is and what they may do."""

from fastapi import APIRouter, Depends, Request
from tenxgraph.core.authz import Identity

from tenxgraph_api.src.app.core.auth.permissions import RequireIdentity
from tenxgraph_api.src.app.utils.response_helper import success_response


router = APIRouter(tags=["Auth"])


@router.get("/v1/me", summary="The authenticated caller")
async def me(request: Request, identity: Identity | None = Depends(RequireIdentity())):
    """``user_id``, ``tenant``, ``roles``, sorted ``permissions`` and ``reach``.

    With auth off there is no identity: ``{"auth": "off"}`` (200), so a UI can show everything.
    An identity without a permission list is unrestricted and reports ``["*"]``.
    """
    if identity is None:
        return success_response({"auth": "off"}, request)
    permissions = ["*"] if identity.permissions is None else sorted(identity.permissions)
    return success_response(
        {
            "user_id": identity.user_id,
            "tenant": identity.tenant,
            "roles": sorted(identity.roles),
            "permissions": permissions,
            "reach": identity.reach.value,
        },
        request,
    )
