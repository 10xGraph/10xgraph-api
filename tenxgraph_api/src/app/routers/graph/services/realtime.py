"""The realtime (live agent) session: init-frame overrides and the event stream."""

from typing import Any

from tenxgraph.core.authz import Identity, UserCredential

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.audit.tools import ToolAudit
from tenxgraph_api.src.app.core.threads import set_user
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe
from tenxgraph_api.src.app.core.validation.request_config import (
    normalize_thread_id,
)


# Realtime init-frame key -> RealtimeConfig field name.
REALTIME_INIT_FIELDS = {
    "model": "model",
    "voice": "voice",
    "modalities": "response_modalities",
    "vad": "vad",
    "system_prompt": "system_instruction",
    "tools_tags": "tools_tags",
}


class RealtimeMixin:
    """``GraphService`` methods for the realtime (live agent) session."""

    async def realtime_graph(
        self,
        input_queue: Any,
        init: dict[str, Any],
        user: Identity | None,
        credential: UserCredential | None = None,
    ):
        """Bridge a realtime (audio) session over the compiled graph.

        Thin wrapper over ``CompiledGraph.arealtime``: builds the per-session config from
        the init control frame + authenticated user, persists thread info, and yields the
        normalized RealtimeEvents. The compiled graph must be rooted at a LiveAgent (e.g.
        an ``AudioAgent``); otherwise ``arealtime`` raises.
        """
        thread_id = normalize_thread_id(init.get("thread_id")) or await self._new_thread_id()
        config: dict[str, Any] = {"thread_id": thread_id}
        set_user(config, user, credential)
        # Map the client init frame onto RealtimeConfig field names so the live agent can
        # apply per-session overrides (model/voice/modalities/vad/...). Only present keys
        # are forwarded; absent ones fall back to the agent's build-time config.
        realtime = self._restrict_realtime_overrides(self._realtime_overrides(init))
        if realtime:
            config["realtime"] = realtime
        await self._save_thread(config, thread_id)
        logger.info("Realtime graph session starting: thread_id=%s", log_safe(thread_id))

        tools = ToolAudit(user, str(thread_id), None)
        async for event in self._graph.arealtime(input_queue, config):
            await tools.record_live(event)
            yield event

        logger.info("Realtime graph session completed: thread_id=%s", log_safe(thread_id))

    def _realtime_overrides(self, init: dict[str, Any]) -> dict[str, Any]:
        """Translate the client init frame into RealtimeConfig field overrides.

        ``model`` and ``tools_tags`` are always mapped and then restricted by
        :meth:`_restrict_realtime_overrides`. The other options are mapped only when listed in
        ``websocket.realtime_client_overrides``; the rest are dropped, so by default a client
        cannot replace the agent's system prompt.
        """
        allowed = set(self.config.websocket.realtime_client_overrides) | {"model", "tools_tags"}
        overrides: dict[str, Any] = {}
        for init_key, field in REALTIME_INIT_FIELDS.items():
            value = init.get(init_key)
            if value is None:
                continue
            if init_key not in allowed:
                logger.info(
                    "Realtime init option %r is not in websocket.realtime_client_overrides; "
                    "ignored",
                    init_key,
                )
                continue
            overrides[field] = value
        # Clients commonly send a single modality as a bare string ("AUDIO"); RealtimeConfig
        # expects a list. Coerce so the shorthand doesn't trip the one-modality validator.
        if isinstance(overrides.get("response_modalities"), str):
            overrides["response_modalities"] = [overrides["response_modalities"]]
        return overrides

    def _live_tools_tags(self) -> set[str] | None:
        """The tag filter the live agent was built with, or ``None`` when it has none."""
        find_live_nodes = getattr(self._graph, "_find_live_nodes", None)
        for _name, node in find_live_nodes() if callable(find_live_nodes) else []:
            realtime_config = getattr(getattr(node, "func", None), "realtime_config", None)
            tags = getattr(realtime_config, "tools_tags", None)
            if tags:
                return {str(tag) for tag in tags}
        return None

    def _restrict_realtime_overrides(self, overrides: dict[str, Any]) -> dict[str, Any]:
        """Drop the init-frame overrides a client is not allowed to make.

        ``model`` is honoured only when listed in ``websocket.realtime_models``. ``tools_tags``
        may only narrow the agent's own filter: widening it, or clearing it (an empty filter
        advertises every tool), would expose tools the agent was built to hide.
        """
        restricted = dict(overrides)
        model = restricted.get("model")
        if model is not None and model not in self.config.websocket.realtime_models:
            logger.info(
                "Realtime init model %r is not in websocket.realtime_models; ignored", model
            )
            restricted.pop("model")

        if "tools_tags" in restricted:
            requested = restricted.pop("tools_tags")
            if isinstance(requested, str):
                requested = [requested]
            if not isinstance(requested, list):
                requested = []
            base = self._live_tools_tags()
            tags = [str(tag) for tag in requested if base is None or str(tag) in base]
            if tags:
                restricted["tools_tags"] = tags
        return restricted

    def _realtime_options(self) -> dict[str, Any]:
        """The /v1/graph/live init options this server applies (see ``_realtime_overrides``)."""
        ws = self.config.websocket
        return {
            "client_overrides": list(ws.realtime_client_overrides),
            "models": list(ws.realtime_models),
            "model_selectable": bool(ws.realtime_models),
            "tools_tags": True,
        }
