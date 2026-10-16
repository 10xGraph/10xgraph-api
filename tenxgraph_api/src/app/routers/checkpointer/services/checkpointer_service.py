from typing import Any

from fastapi import HTTPException
from injectq import inject, singleton
from tenxgraph.core.state import AgentState, Message
from tenxgraph.storage.checkpointer import BaseCheckpointer

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.auth.request_config import (
    client_config,
    client_tool_call_error,
    owned_thread_info,
    reject_unlisted_keys,
)
from tenxgraph_api.src.app.core.config.settings import get_settings
from tenxgraph_api.src.app.core.utils.log_sanitizer import sanitize_for_logging
from tenxgraph_api.src.app.routers.checkpointer.schemas.checkpointer_schemas import (
    MessagesListResponseSchema,
    ResponseSchema,
    StateResponseSchema,
    ThreadResponseSchema,
    ThreadsListResponseSchema,
)
from tenxgraph_api.src.app.routers.graph.services.multimodal_preprocessor import (
    check_media_references,
)
from tenxgraph_api.src.app.utils.parse_output import parse_state_output


class CheckpointerUnavailableError(HTTPException, ValueError):
    """Raised when the checkpointer service has not been configured."""

    def __init__(self) -> None:
        detail = "Checkpointer is not configured"
        HTTPException.__init__(self, status_code=503, detail=detail)
        ValueError.__init__(self, detail)


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
        # The thread's state is the next run's state, so the initial_state allow-list applies.
        try:
            reject_unlisted_keys(state, self._state_keys(), "state")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # context is appended, so every message here is new and client-authored. A tool call
        # in it would run on the next resume without the model ever requesting it.
        if error := client_tool_call_error(state.get("context")):
            raise HTTPException(status_code=422, detail=error)
        await self._check_media_references(state.get("context") or [], user)
        await self._claim_thread(cfg, user)
        old_state: AgentState | None = await self.checkpointer.aget_state(cfg)
        if not old_state:
            old_state = await self.checkpointer.aget_state_cache(cfg)

        # say two states are being merged
        # How to merge to pydantic model
        # Merge incoming state dict into existing Pydantic state, then rebuild the model.

        merged = self._merge_states(old_state, state)
        to_store = self._reconstruct_state(old_state, merged)
        res = await self.checkpointer.aput_state(cfg, to_store)  # type: ignore[arg-type]
        # update cache as well
        await self.checkpointer.aput_state_cache(cfg, to_store)  # type: ignore

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
        await self._check_media_references(messages, user)
        await self._claim_thread(cfg, user)
        res = await self.checkpointer.aput_messages(cfg, messages, metadata)
        return ResponseSchema(success=True, message="Messages put successfully", data=res)

    async def get_message(
        self,
        config: dict[str, Any],
        user: dict,
        message_id: Any,
    ) -> Message:
        cfg = self._config(config, user)
        return await self.checkpointer.aget_message(cfg, message_id)

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
        res = await self.checkpointer.adelete_message(cfg, message_id)
        return ResponseSchema(success=True, message="Message deleted successfully", data=res)

    # Threads
    async def get_thread(self, config: dict[str, Any], user: dict) -> ThreadResponseSchema:
        cfg = self._config(config, user)
        logger.debug(f"User info: {sanitize_for_logging(user)} and thread config: {cfg}")
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
        logger.debug(f"User info: {sanitize_for_logging(user)} and thread ID: {thread_id}")
        res = await self.checkpointer.aclean_thread(cfg)

        # Clean telemetry traces if TelemetryStore is bound in InjectQ
        try:
            from injectq import InjectQ

            from tenxgraph_api.src.app.utils.telemetry_store import TelemetryStore

            telemetry_store = InjectQ.get_instance().try_get(TelemetryStore)
            if telemetry_store:
                telemetry_store.delete_thread(str(thread_id))
        except Exception as exc:
            logger.debug("Telemetry store cleanup failed for thread %s: %s", thread_id, exc)

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
            logger.debug("Ownership cache eviction failed for thread %s: %s", thread_id, exc)

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
