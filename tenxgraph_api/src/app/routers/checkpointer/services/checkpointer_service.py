from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException
from injectq import inject, singleton
from pydantic import BaseModel, ValidationError
from tenxgraph.core.authz import Identity
from tenxgraph.core.exceptions import ThreadNotFoundError
from tenxgraph.core.state import AgentState, Message
from tenxgraph.storage.checkpointer import BaseCheckpointer
from tenxgraph.utils.thread_info import ThreadInfo

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.config.settings import get_settings
from tenxgraph_api.src.app.core.threads import require_thread
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe
from tenxgraph_api.src.app.core.validation.client_filters import (
    check_thread_filters,
    filter_errors_as_client_errors,
)
from tenxgraph_api.src.app.core.validation.request_config import (
    ClientInputError,
    client_config,
    client_message_error,
    owned_thread_info,
    reject_builtin_state_keys,
    reject_unlisted_keys,
    tool_result_ids,
    with_fresh_message_ids,
)
from tenxgraph_api.src.app.routers.checkpointer.schemas.checkpointer_schemas import (
    MessagesListResponseSchema,
    ResponseSchema,
    StateResponseSchema,
    ThreadResponseSchema,
    ThreadsListResponseSchema,
)
from tenxgraph_api.src.app.routers.graph.services.multimodal_preprocessor import (
    check_external_media_urls,
    check_media_references,
    internal_media_keys,
    resolve_media_policy,
)
from tenxgraph_api.src.app.routers.graph.thread_lock import thread_write_guard
from tenxgraph_api.src.app.utils.parse_output import parse_state_output


class CheckpointerUnavailableError(HTTPException, ValueError):
    """Raised when the checkpointer service has not been configured."""

    def __init__(self) -> None:
        detail = "Checkpointer is not configured"
        HTTPException.__init__(self, status_code=503, detail=detail)
        ValueError.__init__(self, detail)


# Messages read per page when collecting a thread's media references for cleanup.
_CLEANUP_PAGE = 500


async def _has_more(
    count: int, offset: int | None, limit: int | None, probe: Callable[[int], Awaitable[list]]
) -> bool:
    """Whether a row exists past this page.

    No checkpointer exposes a total, so a full page triggers a one-row read at the next
    offset. A short page (or an unlimited read) is the end.
    """
    if not limit or count < limit:
        return False
    return bool(await probe((offset or 0) + limit))


@singleton
class CheckpointerService:
    """Thread, state and message routes over the bound checkpointer.

    Every thread route starts with one identity-scoped thread read (:meth:`_require`): a
    thread outside the caller's reach answers 404 (``ThreadNotFoundError``) before any lock is
    taken. Reads and deletes follow the caller's reach; state and message writes always stay
    within the caller's own threads, whatever the reach. Writes take the thread's run lock, so
    they never race a run.
    """

    @inject
    def __init__(self, checkpointer: BaseCheckpointer):
        self.checkpointer = checkpointer
        self.settings = get_settings()

    def _config(self, config: dict[str, Any] | None, identity: Identity | None) -> dict[str, Any]:
        if not self.checkpointer:
            raise CheckpointerUnavailableError()

        # Client keys pass through; server-owned keys (user, internals) never do.
        cfg: dict[str, Any] = client_config(config)
        if identity is not None:
            cfg["user"] = identity
        cfg["user_id"] = identity.user_id if identity is not None else "anonymous"
        return cfg

    async def _require(
        self, identity: Identity | None, thread_id: Any, *, write: bool = False
    ) -> ThreadInfo | None:
        """The caller's thread, or ``ThreadNotFoundError`` (404) when it is outside their reach.

        ``write=True`` reads with reach ``own`` (writes never touch another user's thread) and,
        under ``runs.thread_ids: "client"``, returns None for an id nobody has used yet: the
        caller then creates that thread. Under ``"server"`` (the default) an unknown id is a
        404, with auth on or off.
        """
        if not self.checkpointer:
            raise CheckpointerUnavailableError()
        runs = self._runs()
        server_ids = runs.server_thread_ids if runs is not None else True
        return await require_thread(
            self.checkpointer, identity, thread_id, run=write, server_thread_ids=server_ids
        )

    async def _check_media_references(self, messages: list[Any], identity: Identity | None) -> None:
        """Messages written into a thread are resolved on the next run, so the files they
        reference must belong to the caller, exactly as for graph input."""
        if identity is None or not messages:
            return
        await check_media_references(
            messages, self._media_service(), str(identity.user_id), identity
        )

    def _check_media_urls(self, messages: list[Any]) -> None:
        """External media URLs in written messages get the same policy check as graph input
        (400), since the next run on the thread may make the server fetch them."""
        if not messages:
            return
        graph_service = self._graph_service()
        policy = (
            graph_service.media_policy if graph_service is not None else resolve_media_policy(None)
        )
        check_external_media_urls(messages, policy)

    async def _link_media(
        self, messages: list[Any], cfg: dict[str, Any], identity: Identity | None
    ) -> None:
        """Record the thread on every owned file the written messages reference."""
        media = self._media_service()
        thread_id = cfg.get("thread_id")
        if identity is None or media is None or thread_id is None:
            return
        keys = [k for m in messages for k in internal_media_keys(m)]
        if keys:
            await media.link_threads(keys, str(thread_id), str(identity.user_id), identity)

    def _link_tracking_media(self) -> Any:
        """The media service when it can track thread links, else ``None``. Never raises."""
        media = self._media_service()
        try:
            return media if media is not None and media.supports_links else None
        except Exception as exc:
            logger.debug("Media store unavailable for file cleanup: %s", exc)
            return None

    async def _thread_media_keys(
        self, cfg: dict[str, Any], skip_message_id: Any = None
    ) -> list[str]:
        """Media keys referenced by the thread's messages and state context, minus one message.

        A read failure raises, so a partial list is never used: cleanup can only miss a file
        (it stays), never delete one that is still referenced.
        """
        keys: list[str] = []
        offset = 0
        try:
            # Files linked through PUT state live in the state's context, not in message rows.
            state = await self.checkpointer.aget_state(cfg)
            for message in getattr(state, "context", None) or []:
                if skip_message_id is not None and str(getattr(message, "message_id", None)) == str(
                    skip_message_id
                ):
                    continue
                keys.extend(internal_media_keys(message))
            while True:
                page = await self.checkpointer.alist_messages(cfg, None, offset, _CLEANUP_PAGE)
                for message in page:
                    if skip_message_id is not None and str(message.message_id) == str(
                        skip_message_id
                    ):
                        continue
                    keys.extend(internal_media_keys(message))
                if len(page) < _CLEANUP_PAGE:
                    return keys
                offset += len(page)
        except Exception as exc:
            logger.warning("Could not read thread messages for file cleanup: %s", exc)
            raise

    async def _check_context_tool_results(self, context: list[Any], cfg: dict[str, Any]) -> None:
        """Hold tool results in client ``context`` to the rule invoke applies: only as the
        answer to a pending remote tool call (``GraphService._check_tool_results``)."""
        if not tool_result_ids(context):
            return
        graph_service = self._graph_service()
        try:
            if graph_service is None:
                raise ClientInputError("tool results cannot be written to a thread directly")
            await graph_service._check_tool_results(context, cfg)
        except ClientInputError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @staticmethod
    def _graph_service() -> Any:
        try:
            from injectq import InjectQ

            from tenxgraph_api.src.app.routers.graph.services.graph_service import GraphService

            return InjectQ.get_instance().try_get(GraphService)
        except Exception:
            return None

    @staticmethod
    def _runs() -> Any:
        """``runs`` from 10xgraph.json (a ``RunsConfig``), or ``None`` when unavailable."""
        try:
            from tenxgraph_api.src.app.core.config.graph_config import GraphConfig, RunsConfig
            from tenxgraph_api.src.app.core.utils.container import current_container

            graph_config = current_container().try_get(GraphConfig)
            runs = getattr(graph_config, "runs", None)
        except Exception:
            return None
        return runs if isinstance(runs, RunsConfig) else None

    @staticmethod
    def _media_service() -> Any:
        try:
            from injectq import InjectQ

            from tenxgraph_api.src.app.routers.media import MediaService

            return InjectQ.get_instance().try_get(MediaService)
        except Exception:
            return None

    async def get_state(
        self, config: dict[str, Any], identity: Identity | None
    ) -> StateResponseSchema:
        cfg = self._config(config, identity)
        await self._require(identity, cfg["thread_id"])

        # this will return base pydantic model
        res = await self.checkpointer.aget_state(cfg)
        if not res:
            rs = await self.checkpointer.aget_state_cache(cfg)
            if isinstance(rs, BaseModel):
                rs = parse_state_output(self.settings, rs)
            return StateResponseSchema(state=rs)

        if res:
            return StateResponseSchema(
                state=parse_state_output(
                    self.settings,
                    res,
                ),
            )

        return StateResponseSchema(state=res)

    async def put_state(
        self,
        config: dict[str, Any],
        identity: Identity | None,
        state: dict[str, Any],
    ) -> StateResponseSchema:
        cfg = self._config(config, identity)
        thread_id = cfg["thread_id"]
        thread = await self._require(identity, thread_id, write=True)
        async with thread_write_guard(thread_id):
            return await self._put_state(cfg, identity, state, new_thread=thread is None)

    async def _put_state(
        self,
        cfg: dict[str, Any],
        identity: Identity | None,
        state: dict[str, Any],
        *,
        new_thread: bool,
    ) -> StateResponseSchema:
        # The built-in fields are never client-writable; the thread's state is the next run's
        # state, so the initial_state allow-list applies to the rest.
        runs = self._runs()
        try:
            reject_builtin_state_keys(state, "state")
            reject_unlisted_keys(state, runs.state_keys if runs is not None else None, "state")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # context is appended, so every message here is new and client-authored: user messages
        # only, and no tool call, which would run on the next resume without the model ever
        # requesting it. A tool result is accepted only as the answer to a pending remote call.
        context = state.get("context")
        if context is not None and not (
            isinstance(context, list) and all(isinstance(m, dict | Message) for m in context)
        ):
            raise HTTPException(
                status_code=422, detail="Invalid state fields: context must be a list of messages"
            )
        if error := client_message_error(context):
            raise HTTPException(status_code=422, detail=error)
        if context:
            await self._check_context_tool_results(context, cfg)
            state = {**state, "context": with_fresh_message_ids(context)}
        self._check_media_urls(state.get("context") or [])
        await self._check_media_references(state.get("context") or [], identity)
        if new_thread:
            # A client-named new thread (runs.thread_ids "client"): register it first, so the
            # caller owns it and it is listed. Of two callers racing for one id, the loser gets
            # ThreadNotFoundError (404) from storage.
            await self.checkpointer.aput_thread(cfg, owned_thread_info(cfg, cfg["thread_id"]))
        old_state: AgentState | None = await self.checkpointer.aget_state(cfg)
        if not old_state:
            old_state = await self.checkpointer.aget_state_cache(cfg)

        # say two states are being merged
        # How to merge to pydantic model
        # Merge incoming state dict into existing Pydantic state, then rebuild the model.

        merged = self._merge_states(old_state, state)
        try:
            to_store = self._reconstruct_state(old_state, merged)
        except ValidationError as exc:
            # Name the offending fields, never echo the values.
            fields = ", ".join(sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()}))
            raise HTTPException(status_code=422, detail=f"Invalid state fields: {fields}") from exc
        res = await self.checkpointer.aput_state(cfg, to_store)  # type: ignore[arg-type]
        # update cache as well
        await self.checkpointer.aput_state_cache(cfg, to_store)  # type: ignore
        await self._link_media(state.get("context") or [], cfg, identity)

        return StateResponseSchema(
            state=parse_state_output(
                self.settings,
                res,
            ),
        )

    async def clear_state(
        self, config: dict[str, Any], identity: Identity | None
    ) -> ResponseSchema:
        cfg = self._config(config, identity)
        await self._require(identity, cfg["thread_id"])
        async with thread_write_guard(cfg["thread_id"]):
            res = await self.checkpointer.aclear_state(cfg)
        return ResponseSchema(success=True, message="State cleared successfully", data=res)

    # Messages
    async def get_message(
        self,
        config: dict[str, Any],
        identity: Identity | None,
        message_id: Any,
    ) -> Message:
        cfg = self._config(config, identity)
        await self._require(identity, cfg["thread_id"])
        return await self._read_message(cfg, message_id)

    async def _read_message(self, cfg: dict[str, Any], message_id: Any) -> Message:
        """The message, or a 404 when the thread has none with that id.

        Checkpointers disagree on a missing message: in-memory and SQLite raise ``IndexError``,
        Postgres raises ``ValueError("Message not found: ...")``. Any other error is not a
        client mistake and passes through.
        """
        try:
            message = await self.checkpointer.aget_message(cfg, message_id)
        except IndexError as exc:
            raise HTTPException(status_code=404, detail="Message not found") from exc
        except ValueError as exc:
            if str(exc).startswith("Message not found"):
                raise HTTPException(status_code=404, detail="Message not found") from exc
            raise
        if message is None:
            raise HTTPException(status_code=404, detail="Message not found")
        return message

    async def update_message_feedback(
        self,
        config: dict[str, Any],
        identity: Identity | None,
        message_id: Any,
        feedback: dict[str, Any],
    ) -> Message:
        """Set ``metadata["feedback"]`` on a message; nothing else on the message changes.

        Raises:
            HTTPException: 501 when the checkpointer lacks ``aupdate_message_metadata``,
                404 when the message is not in this thread.
            ThreadNotFoundError: The thread is not the caller's own (a write, so reach
                ``tenant`` or ``all`` does not widen it).
        """
        cfg = self._config(config, identity)
        update = getattr(self.checkpointer, "aupdate_message_metadata", None)
        if not callable(update):
            raise HTTPException(
                status_code=501,
                detail=(
                    "This checkpointer does not support message feedback "
                    "(needs aupdate_message_metadata, added in 10xgraph 1.0)"
                ),
            )
        if await self._require(identity, cfg["thread_id"], write=True) is None:
            raise HTTPException(status_code=404, detail="Message not found")
        message = await update(cfg, message_id, {"feedback": feedback})
        if message is None:
            raise HTTPException(status_code=404, detail="Message not found")
        return message

    async def get_messages(
        self,
        config: dict[str, Any],
        identity: Identity | None,
        search: str | None = None,
        offset: int | None = None,
        limit: int | None = None,
    ) -> MessagesListResponseSchema:
        cfg = self._config(config, identity)
        await self._require(identity, cfg["thread_id"])
        res = await self.checkpointer.alist_messages(cfg, search, offset, limit)
        has_more = await _has_more(
            len(res), offset, limit, lambda o: self.checkpointer.alist_messages(cfg, search, o, 1)
        )
        return MessagesListResponseSchema(
            messages=res, has_more=has_more, offset=offset or 0, limit=limit
        )

    async def delete_message(
        self,
        config: dict[str, Any],
        identity: Identity | None,
        message_id: Any,
    ) -> ResponseSchema:
        cfg = self._config(config, identity)
        await self._require(identity, cfg["thread_id"])
        async with thread_write_guard(cfg["thread_id"]):
            return await self._delete_message(cfg, message_id)

    async def _delete_message(self, cfg: dict[str, Any], message_id: Any) -> ResponseSchema:
        # Read first: a missing message is a 404 on every checkpointer, and its files are
        # needed for the cleanup below. Any other read failure just leaves the files in place.
        message: Message | None = None
        try:
            message = await self._read_message(cfg, message_id)
        except HTTPException:
            raise
        except Exception as exc:
            logger.debug("File cleanup skipped for message %s: %s", log_safe(message_id), exc)
        released: list[str] = []
        media = self._link_tracking_media()
        if media is not None and message is not None:
            try:
                gone = internal_media_keys(message)
                if gone:
                    kept = set(await self._thread_media_keys(cfg, skip_message_id=message_id))
                    released = [k for k in dict.fromkeys(gone) if k not in kept]
            except Exception as exc:
                logger.debug("File cleanup skipped for message %s: %s", log_safe(message_id), exc)
        res = await self.checkpointer.adelete_message(cfg, message_id)
        if not res:
            # Nothing deleted (gone meanwhile): no file is released.
            raise HTTPException(status_code=404, detail="Message not found")
        if released and media is not None and cfg.get("thread_id") is not None:
            await media.unlink_threads(released, str(cfg["thread_id"]))
        return ResponseSchema(success=True, message="Message deleted successfully", data=res)

    # Threads
    async def get_thread(
        self, config: dict[str, Any], identity: Identity | None
    ) -> ThreadResponseSchema:
        cfg = self._config(config, identity)
        logger.debug(
            "User info: %s and thread config: %s",
            log_safe(identity.user_id if identity else None),
            log_safe(cfg),
        )
        res = await self._require(identity, cfg["thread_id"])
        return ThreadResponseSchema(thread=res.model_dump() if res else None)

    async def list_threads(
        self,
        identity: Identity | None,
        search: str | None = None,
        offset: int | None = None,
        limit: int | None = None,
        filters: dict[str, Any] | None = None,
    ) -> ThreadsListResponseSchema:
        """List the caller's threads, narrowed by ``filters`` (validated first, 422 when bad).

        ``filters`` only narrow: core ANDs them after the isolation conditions, which come from
        the ``Identity`` in ``config["user"]``, never from the client.
        """
        cfg = self._config({}, identity)
        check_thread_filters(self.checkpointer, filters)
        # Only pass filters when there are some, so a custom checkpointer written before
        # ``alist_threads`` took ``filters`` keeps working for plain listings.
        extra: dict[str, Any] = {"filters": filters} if filters else {}
        with filter_errors_as_client_errors():
            res = await self.checkpointer.alist_threads(cfg, search, offset, limit, **extra)
            has_more = await _has_more(
                len(res),
                offset,
                limit,
                lambda o: self.checkpointer.alist_threads(cfg, search, o, 1, **extra),
            )
        return ThreadsListResponseSchema(
            threads=[t.model_dump() for t in res],
            has_more=has_more,
            offset=offset or 0,
            limit=limit,
        )

    async def delete_thread(
        self,
        config: dict[str, Any],
        identity: Identity | None,
        thread_id: Any,
    ) -> ResponseSchema:
        """Delete a thread, then release its files and drop its traces.

        Follows the caller's reach. The cleanup runs only after storage reports the thread
        deleted, and only for the owner the scoped read returned: a delete that removed nothing
        (outside the caller's reach, or gone meanwhile) is a 404 and touches no file and no
        trace. A deleted id can be created again by anyone.
        """
        cfg = self._config(config, identity)
        logger.debug(
            "User info: %s and thread ID: %s",
            log_safe(identity.user_id if identity else None),
            log_safe(thread_id),
        )
        thread = await self._require(identity, thread_id)
        async with thread_write_guard(thread_id):
            return await self._delete_thread(cfg, identity, thread_id, thread)

    async def _delete_thread(
        self,
        cfg: dict[str, Any],
        identity: Identity | None,
        thread_id: Any,
        thread: ThreadInfo | None,
    ) -> ResponseSchema:
        # Cleanup after the thread row is gone is best effort, but a delete that left data
        # behind must not read as a clean success: each step that failed is named in the reply.
        incomplete: list[str] = []
        # Collect the files the thread's messages reference before they are gone.
        thread_keys: list[str] = []
        media = self._link_tracking_media()
        if media is not None:
            try:
                thread_keys = list(dict.fromkeys(await self._thread_media_keys(cfg)))
            except Exception as exc:
                incomplete.append("files")
                logger.warning("File cleanup skipped for thread %s: %s", log_safe(thread_id), exc)
        res = await self.checkpointer.aclean_thread(cfg)
        if not res:
            raise ThreadNotFoundError(thread_id)
        if (
            thread_keys
            and media is not None
            and await media.unlink_threads(thread_keys, str(thread_id))
        ):
            incomplete.append("files")
        # Files still linked to the thread through messages that are gone (cleared or replaced
        # state) are not in `thread_keys`; release the owner's by their own metadata.
        owner = thread.user_id if thread is not None else None
        if (
            media is not None
            and owner is not None
            and await media.release_thread(str(thread_id), str(owner), identity)
        ):
            incomplete.append("files")

        # Clean telemetry traces if TelemetryStore is bound in InjectQ
        try:
            from injectq import InjectQ

            from tenxgraph_api.src.app.utils.telemetry_store import TelemetryStore

            telemetry_store = InjectQ.get_instance().try_get(TelemetryStore)
            if telemetry_store:
                telemetry_store.delete_thread(str(thread_id))
        except Exception as exc:
            incomplete.append("traces")
            logger.warning(
                "Telemetry store cleanup failed for thread %s: %s", log_safe(thread_id), exc
            )

        if incomplete:
            steps = ", ".join(dict.fromkeys(incomplete))
            return ResponseSchema(
                success=False,
                message=f"Thread deleted, but cleanup did not finish: {steps}",
                data={
                    "deleted": True,
                    "cleanup_failed": list(dict.fromkeys(incomplete)),
                    "result": res,
                },
            )
        return ResponseSchema(success=True, message="Thread deleted successfully", data=res)

    # -------------------------------------------------
    # Internal helpers
    # -------------------------------------------------
    def _merge_states(
        self, old_state: AgentState | None, updates: dict[str, Any]
    ) -> dict[str, Any]:
        """Merge a partial state dict into an existing AgentState.

        Rules:
        - Preserve execution_meta from old_state unless explicitly provided (discouraged).
        - Append context messages if both sides provide them; otherwise set from updates.
        - Deep-merge dictionaries; non-dict values from updates overwrite.
        - None values in updates do not overwrite existing values.
        """

        base: dict[str, Any] = {}
        if old_state is not None:
            # Keep full dump so we can preserve existing fields
            # Use serialize_as_any=True to include subclass fields
            base = old_state.model_dump(serialize_as_any=True)

        merged: dict[str, Any] = {**base}

        # Handle context specially (append)
        if "context" in updates and updates["context"] is not None:
            old_ctx = base.get("context", []) if base else []
            new_ctx = updates.get("context") or []
            # Simply concatenate; Pydantic will validate/convert dicts to Message
            merged["context"] = list(old_ctx) + list(new_ctx)

        # execution_meta: keep from old unless explicitly provided as dict/model
        if old_state is not None:
            merged["execution_meta"] = old_state.execution_meta

        # Apply remaining fields with deep merge
        for k, v in updates.items():
            if k in ("context", "execution_meta"):
                continue
            if v is None:
                # Do not erase existing values with None by default
                continue
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = self._deep_merge_dicts(merged[k], v)
            else:
                merged[k] = v

        return merged

    def _deep_merge_dicts(self, base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
        """Deep merge two dictionaries without mutating inputs."""
        out: dict[str, Any] = {**base}
        for k, v in updates.items():
            if v is None:
                continue
            if k in out and isinstance(out[k], dict) and isinstance(v, dict):
                out[k] = self._deep_merge_dicts(out[k], v)
            else:
                out[k] = v
        return out

    def _reconstruct_state(self, old_state: AgentState | None, data: dict[str, Any]) -> AgentState:
        """Rebuild the appropriate AgentState (or subclass) from merged dict."""
        state_cls = type(old_state) if isinstance(old_state, AgentState) else AgentState

        # Ensure execution_meta stays as model if present
        if old_state is not None:
            data["execution_meta"] = old_state.execution_meta

        return state_cls.model_validate(data)
