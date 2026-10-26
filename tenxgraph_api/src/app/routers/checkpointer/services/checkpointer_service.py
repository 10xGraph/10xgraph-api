from typing import Any

from fastapi import HTTPException
from injectq import inject, singleton
from pydantic import ValidationError
from tenxgraph.core.state import AgentState, Message
from tenxgraph.storage.checkpointer import BaseCheckpointer

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.auth.request_config import (
    ClientInputError,
    client_config,
    client_message_error,
    owned_thread_info,
    reject_builtin_state_keys,
    reject_unlisted_keys,
    tool_result_ids,
    with_fresh_message_ids,
)
from tenxgraph_api.src.app.core.config.settings import get_settings
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe, sanitize_for_logging
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
from tenxgraph_api.src.app.utils.parse_output import parse_state_output


class CheckpointerUnavailableError(HTTPException, ValueError):
    """Raised when the checkpointer service has not been configured."""

    def __init__(self) -> None:
        detail = "Checkpointer is not configured"
        HTTPException.__init__(self, status_code=503, detail=detail)
        ValueError.__init__(self, detail)


# Messages read per page when collecting a thread's media references for cleanup.
_CLEANUP_PAGE = 500


@singleton
class CheckpointerService:
    @inject
    def __init__(self, checkpointer: BaseCheckpointer):
        self.checkpointer = checkpointer
        self.settings = get_settings()

    def _config(self, config: dict[str, Any] | None, user: dict) -> dict[str, Any]:
        if not self.checkpointer:
            raise CheckpointerUnavailableError()

        # Client keys pass through; server-owned keys (authz, user, internals) never do.
        cfg: dict[str, Any] = client_config(config)
        cfg["user"] = user
        cfg["user_id"] = user.get("user_id", "anonymous")
        return cfg

    async def _claim_thread(self, cfg: dict[str, Any], user: dict) -> None:
        """Record the caller as the owner of a thread nobody owns yet, before writing to it.

        Ownership authorization lets a write through to an unowned thread on the promise that
        the writer becomes its owner. State and message writes do not register the thread on
        their own, so without this the thread would stay ownerless and stay open to everyone.
        """
        thread_id = cfg.get("thread_id")
        if not user.get("user_id") or thread_id is None:
            return
        try:
            owner = await self.checkpointer.aget_thread_owner(thread_id)
        except NotImplementedError:
            return  # this checkpointer keeps no owners; ownership authz refuses to boot on it
        if owner is None:
            await self.checkpointer.aput_thread(cfg, owned_thread_info(cfg, thread_id))

    async def _check_media_references(self, messages: list[Any], user: dict) -> None:
        """Messages written into a thread are resolved on the next run, so the files they
        reference must belong to the caller, exactly as for graph input."""
        user_id = user.get("user_id")
        if not user_id or not messages:
            return
        await check_media_references(messages, self._media_service(), str(user_id))

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

    async def _link_media(self, messages: list[Any], cfg: dict[str, Any], user: dict) -> None:
        """Record the thread on every owned file the written messages reference."""
        user_id = user.get("user_id")
        media = self._media_service()
        thread_id = cfg.get("thread_id")
        if not user_id or media is None or thread_id is None:
            return
        keys = [k for m in messages for k in internal_media_keys(m)]
        if keys:
            await media.link_threads(keys, str(thread_id), str(user_id))

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
        """Media keys referenced by the thread's messages (all pages), minus one message.

        A read failure raises, so a partial list is never used: cleanup can only miss a file
        (it stays), never delete one that is still referenced.
        """
        keys: list[str] = []
        offset = 0
        try:
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
    def _state_keys() -> frozenset[str] | None:
        """``runs.state_keys`` from 10xgraph.json, or ``None`` when unset or unavailable."""
        try:
            from injectq import InjectQ

            from tenxgraph_api.src.app.core.config.graph_config import GraphConfig, RunsConfig

            graph_config = InjectQ.get_instance().try_get(GraphConfig)
            runs = getattr(graph_config, "runs", None)
        except Exception:
            return None
        return runs.state_keys if isinstance(runs, RunsConfig) else None

    @staticmethod
    def _media_service() -> Any:
        try:
            from injectq import InjectQ

            from tenxgraph_api.src.app.routers.media import MediaService

            return InjectQ.get_instance().try_get(MediaService)
        except Exception:
            return None

    async def get_state(self, config: dict[str, Any], user: dict) -> StateResponseSchema:
        cfg = self._config(config, user)

        # this will return base pydantic model
        res = await self.checkpointer.aget_state(cfg)
        if not res:
            rs = await self.checkpointer.aget_state_cache(cfg)
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
        user: dict,
        state: dict[str, Any],
    ) -> StateResponseSchema:
        cfg = self._config(config, user)
        # The built-in fields are never client-writable; the thread's state is the next run's
        # state, so the initial_state allow-list applies to the rest.
        try:
            reject_builtin_state_keys(state, "state")
            reject_unlisted_keys(state, self._state_keys(), "state")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # context is appended, so every message here is new and client-authored: user messages
        # only, and no tool call, which would run on the next resume without the model ever
        # requesting it. A tool result is accepted only as the answer to a pending remote call.
        context = state.get("context")
        if error := client_message_error(context):
            raise HTTPException(status_code=422, detail=error)
        if context:
            await self._check_context_tool_results(context, cfg)
            state = {**state, "context": with_fresh_message_ids(context)}
        self._check_media_urls(state.get("context") or [])
        await self._check_media_references(state.get("context") or [], user)
        await self._claim_thread(cfg, user)
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
        await self._link_media(state.get("context") or [], cfg, user)

        return StateResponseSchema(
            state=parse_state_output(
                self.settings,
                res,
            ),
        )

    async def clear_state(self, config: dict[str, Any], user: dict) -> ResponseSchema:
        cfg = self._config(config, user)
        res = await self.checkpointer.aclear_state(cfg)
        return ResponseSchema(success=True, message="State cleared successfully", data=res)

    # Messages
    async def put_messages(
        self,
        config: dict[str, Any],
        user: dict,
        messages: list[Message],
        metadata: dict[str, Any] | None = None,
    ) -> ResponseSchema:
        cfg = self._config(config, user)
        self._check_media_urls(messages)
        await self._check_media_references(messages, user)
        await self._claim_thread(cfg, user)
        res = await self.checkpointer.aput_messages(cfg, messages, metadata)
        await self._link_media(messages, cfg, user)
        return ResponseSchema(success=True, message="Messages put successfully", data=res)

    async def get_message(
        self,
        config: dict[str, Any],
        user: dict,
        message_id: Any,
    ) -> Message:
        cfg = self._config(config, user)
        return await self.checkpointer.aget_message(cfg, message_id)

    async def update_message_feedback(
        self,
        config: dict[str, Any],
        user: dict,
        message_id: Any,
        feedback: dict[str, Any],
    ) -> Message:
        """Set ``metadata["feedback"]`` on a message; nothing else on the message changes.

        Raises:
            HTTPException: 501 when the checkpointer lacks ``aupdate_message_metadata`` (core
                older than 0.10.4), 404 when the message is not in this thread.
        """
        cfg = self._config(config, user)
        update = getattr(self.checkpointer, "aupdate_message_metadata", None)
        if not callable(update):
            raise HTTPException(
                status_code=501,
                detail="Updating message feedback needs 10xgraph >= 0.10.4",
            )
        message = await update(cfg, message_id, {"feedback": feedback})
        if message is None:
            raise HTTPException(status_code=404, detail="Message not found")
        return message

    async def get_messages(
        self,
        config: dict[str, Any],
        user: dict,
        search: str | None = None,
        offset: int | None = None,
        limit: int | None = None,
    ) -> MessagesListResponseSchema:
        cfg = self._config(config, user)
        res = await self.checkpointer.alist_messages(cfg, search, offset, limit)
        return MessagesListResponseSchema(messages=res)

    async def delete_message(
        self,
        config: dict[str, Any],
        user: dict,
        message_id: Any,
    ) -> ResponseSchema:
        cfg = self._config(config, user)
        # Files only this message references lose their link to the thread. Read before the
        # delete; a read failure just leaves the files in place.
        released: list[str] = []
        media = self._link_tracking_media()
        if media is not None:
            try:
                gone = internal_media_keys(await self.checkpointer.aget_message(cfg, message_id))
                if gone:
                    kept = set(await self._thread_media_keys(cfg, skip_message_id=message_id))
                    released = [k for k in dict.fromkeys(gone) if k not in kept]
            except Exception as exc:
                logger.debug("File cleanup skipped for message %s: %s", log_safe(message_id), exc)
        res = await self.checkpointer.adelete_message(cfg, message_id)
        if released and media is not None and cfg.get("thread_id") is not None:
            await media.unlink_threads(released, str(cfg["thread_id"]))
        return ResponseSchema(success=True, message="Message deleted successfully", data=res)

    # Threads
    async def get_thread(self, config: dict[str, Any], user: dict) -> ThreadResponseSchema:
        cfg = self._config(config, user)
        logger.debug(
            "User info: %s and thread config: %s",
            log_safe(sanitize_for_logging(user)),
            log_safe(cfg),
        )
        res = await self.checkpointer.aget_thread(cfg)
        return ThreadResponseSchema(thread=res.model_dump() if res else None)

    async def list_threads(
        self,
        user: dict,
        search: str | None = None,
        offset: int | None = None,
        limit: int | None = None,
    ) -> ThreadsListResponseSchema:
        cfg = self._config({}, user)
        res = await self.checkpointer.alist_threads(cfg, search, offset, limit)
        return ThreadsListResponseSchema(threads=[t.model_dump() for t in res])

    async def delete_thread(
        self,
        config: dict[str, Any],
        user: dict,
        thread_id: Any,
    ) -> ResponseSchema:
        cfg = self._config(config, user)
        logger.debug(
            "User info: %s and thread ID: %s",
            log_safe(sanitize_for_logging(user)),
            log_safe(thread_id),
        )
        # Collect the files the thread's messages reference before they are gone.
        thread_keys: list[str] = []
        media = self._link_tracking_media()
        if media is not None:
            try:
                thread_keys = list(dict.fromkeys(await self._thread_media_keys(cfg)))
            except Exception as exc:
                logger.debug("File cleanup skipped for thread %s: %s", log_safe(thread_id), exc)
        res = await self.checkpointer.aclean_thread(cfg)
        if thread_keys and media is not None:
            await media.unlink_threads(thread_keys, str(thread_id))

        # Clean telemetry traces if TelemetryStore is bound in InjectQ
        try:
            from injectq import InjectQ

            from tenxgraph_api.src.app.utils.telemetry_store import TelemetryStore

            telemetry_store = InjectQ.get_instance().try_get(TelemetryStore)
            if telemetry_store:
                telemetry_store.delete_thread(str(thread_id))
        except Exception as exc:
            logger.debug(
                "Telemetry store cleanup failed for thread %s: %s", log_safe(thread_id), exc
            )

        # Invalidate any cached ownership for this thread. Ownership is otherwise
        # immutable, so deletion is the only event that must evict the cache.
        try:
            from tenxgraph_api.src.app.core.auth.authorization import AuthorizationBackend
            from tenxgraph_api.src.app.core.utils.container import current_container

            authz = current_container().try_get(AuthorizationBackend)
            evict = getattr(authz, "evict", None)
            if callable(evict):
                pending = evict(str(thread_id), user.get("user_id"))
                if pending is not None:
                    await pending
        except Exception as exc:
            logger.debug(
                "Ownership cache eviction failed for thread %s: %s", log_safe(thread_id), exc
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
