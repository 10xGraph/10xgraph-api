"""Refuse the reserved owners of shared data as a caller identity.

Core keeps shared, ownerless data (agent/app memory, RAG knowledge bases) under reserved owners
``system_user_id(name)`` = ``"__system__:<name>"``. If a caller could authenticate as one of
them, every store route would read and write that shared data as its owner, so no identity whose
id starts with the reserved prefix is ever accepted, whatever auth backend produced it.
"""

from __future__ import annotations

from typing import Any

from tenxgraph.core.authz import Identity

from tenxgraph_api.src.app.core import logger


try:
    from tenxgraph.storage.store import SYSTEM_USER_PREFIX
except ImportError:  # core older than the release that added the constant
    SYSTEM_USER_PREFIX = "__system__"

RESERVED_USER_ID_MESSAGE = "This user id is reserved"


def is_reserved_user_id(value: Any) -> bool:
    """Whether ``value`` is a reserved owner id, after the API's own normalisation.

    Any id starting with the reserved prefix is refused (the bare prefix, ``"__system__:<name>"``
    and anything else that starts with it). Ints are stored as their string form (see
    ``JwtAuth``) and can never match; surrounding whitespace is ignored so a padded copy cannot
    slip past the check. Booleans are never user ids.
    """
    if isinstance(value, bool) or not isinstance(value, str | int):
        return False
    return str(value).strip().startswith(SYSTEM_USER_PREFIX)


def identity_is_reserved(identity: Identity | dict[str, Any]) -> bool:
    """Whether a caller (an ``Identity``, or the claims dict an auth class returns) is reserved.

    Anything else raises ``TypeError``: a check that answers "not reserved" for a type it does
    not understand would fail open.
    """
    if not isinstance(identity, Identity | dict):
        raise TypeError(f"expected Identity or dict, got {type(identity).__name__}")
    return is_reserved_user_id(identity.get("user_id"))


def log_reserved_rejection() -> None:
    """Warn about a rejected identity without echoing the reserved name."""
    logger.warning("Rejected authenticated identity that uses the reserved shared-data user id")
