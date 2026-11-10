"""The one identity-scoped thread read every thread route makes before touching a thread.

Storage filters every query by the caller's reach, so a thread the caller cannot see and a
thread that does not exist look the same: ``aget_thread`` returns None. This read turns that
into a 404 before any lock is taken or any response header is sent, and enforces
``runs.thread_ids: "server"`` (a client may not name a new thread id).
"""

from __future__ import annotations

from typing import Any

from tenxgraph.core.authz import Identity, Reach
from tenxgraph.core.exceptions import ThreadNotFoundError
from tenxgraph.storage.checkpointer import BaseCheckpointer
from tenxgraph.utils.thread_info import ThreadInfo

from tenxgraph_api.src.app.core.audit import audit_not_found


async def require_thread(
    checkpointer: BaseCheckpointer,
    identity: Identity | None,
    thread_id: Any,
    *,
    run: bool = False,
    server_thread_ids: bool = True,
) -> ThreadInfo | None:
    """Return the caller's thread, or raise :class:`ThreadNotFoundError` (404).

    ``run=True`` is for runs and state/message writes. They always stay within the caller's
    own threads, whatever their reach (nobody runs in another user's thread), so the read
    uses reach ``own``. An unknown id then returns None (the caller creates the thread) unless
    ``server_thread_ids`` is set, in which case only ids the server created are accepted.

    ``run=False`` is for reads and deletes, which follow the caller's reach. An unknown or
    invisible id is always a 404.
    """
    if run and identity is not None and identity.reach is not Reach.OWN:
        identity = identity.model_copy(update={"reach": Reach.OWN})
    config: dict[str, Any] = {
        "thread_id": thread_id,
        "user_id": identity.user_id if identity is not None else "anonymous",
    }
    if identity is not None:
        config["user"] = identity
    thread = await checkpointer.aget_thread(config)
    if thread is not None:
        return thread
    if run and not server_thread_ids and not await _taken(checkpointer, thread_id):
        return None
    error = ThreadNotFoundError(thread_id)
    await audit_not_found(error, identity)  # one event, on every transport
    raise error


async def _taken(checkpointer: BaseCheckpointer, thread_id: Any) -> bool:
    """Whether anyone has a thread under ``thread_id``, read globally (not by reach).

    The scoped read above returns None for a free id and for another user's thread alike.
    With ``runs.thread_ids: "client"`` a free id is created and a taken one refused, so this
    tells nothing a client could not learn anyway, and it turns the taken case into a 404
    before any lock or response header. A backend that cannot tell answers no: storage still
    refuses the write to a foreign thread (a 404 on invoke, an error chunk on a stream).
    """
    try:
        return await checkpointer.aget_thread_owner(thread_id) is not None
    except NotImplementedError:
        return False
