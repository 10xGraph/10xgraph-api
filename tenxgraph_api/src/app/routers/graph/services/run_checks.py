"""Run input checks and thread resolution: media, model choice, resume and tool results."""

import inspect
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

from injectq import InjectQ
from pydantic import TypeAdapter, ValidationError
from tenxgraph.core.authz import Identity, UserCredential
from tenxgraph.core.exceptions import ModelNotFoundError
from tenxgraph.core.graph import ToolNode
from tenxgraph.utils.interrupt import pending_interrupt

from tenxgraph_api.src.app.core import logger
from tenxgraph_api.src.app.core.config.graph_config import RunsConfig
from tenxgraph_api.src.app.core.exceptions.client_errors import (
    as_http_error,
)
from tenxgraph_api.src.app.core.exceptions.client_errors import (
    resume_error_types as _resume_error_types,
)
from tenxgraph_api.src.app.core.threads import require_thread, set_user
from tenxgraph_api.src.app.core.utils.container import lookup
from tenxgraph_api.src.app.core.validation.request_config import (
    ALWAYS_ALLOWED_CONFIG_KEYS,
    ClientInputError,
    InterruptConflictError,
    client_config,
    normalize_thread_id,
    reject_builtin_state_keys,
    reject_unlisted_keys,
    tool_result_ids,
    unanswered_tool_calls,
)
from tenxgraph_api.src.app.routers.graph.schemas.graph_schemas import (
    GraphInputSchema,
)
from tenxgraph_api.src.app.routers.graph.services.multimodal_preprocessor import (
    check_external_media_urls,
    internal_media_keys,
    preprocess_multimodal_messages,
    resolve_media_policy,
)
from tenxgraph_api.src.app.routers.graph.services.run_models import (
    _UNREAD,
    RunChecks,
    _resume_http_error,
    _run_tool_names,
)


# A taken generated id is retried with a fresh one; each retry is another prediction the
# claimer would have had to get right.
MAX_THREAD_ID_ATTEMPTS = 5


class RunChecksMixin:
    """``GraphService`` methods that check a run's input and resolve its thread."""

    _model_choices: dict[str, dict[str, Any]] | None = None

    @property
    def media_service(self):
        if self._media_service is None:
            from tenxgraph_api.src.app.routers.media import MediaService

            self._media_service = lookup(MediaService)
        return self._media_service

    @property
    def media_policy(self) -> Any:
        """The graph's media fetch policy (``None`` when the installed core has none)."""
        return resolve_media_policy(self._graph)

    def check_media_urls(self, messages: list[Any]) -> None:
        """Reject external media URLs the graph's fetch policy blocks (400, no network)."""
        check_external_media_urls(messages, self.media_policy)

    async def check_inline_media(self, messages: list[Any], user: Identity | None) -> None:
        """Refuse inline media the run would store but an upload would refuse (422).

        The run stores the same blobs and checks them again; this runs before a stream's
        response starts, so a refusal is a real 422 and not an error chunk. The status, code
        and message are those of ``client_errors.as_http_error``, as for the in-run refusal.
        """
        check = getattr(self.media_service, "check_inline", None)
        if not inspect.iscoroutinefunction(check):
            return
        try:
            await check(messages, user.user_id if user else None, user)
        except ClientInputError as exc:
            # The answer the run gives for the same error (client_errors), on every transport.
            raise as_http_error(exc) from exc

    def check_model_choice(self, config: Any) -> None:
        """Reject a ``config[config_key]`` that is not a registered model name of its node.

        Runs before a run starts, so a stream answers a real 422, not an error chunk.
        """
        if self._model_choices is None:
            info = self._graph.generate_graph().get("info")
            models = info.get("models") if isinstance(info, dict) else None
            self._model_choices = models if isinstance(models, dict) else {}
        config = client_config(config)
        for node_name, choice in self._model_choices.items():
            key = choice["config_key"]
            # None means the node's default model, as in core.
            if config.get(key) is not None and config[key] not in choice["names"]:
                raise ModelNotFoundError(config[key], node_name, choice["names"])

    def check_run_input(self, messages: list[Any], config: Any) -> RunChecks:
        """The media URL policy (400) and the model choice (422), before anything is held.

        Routers call this first and hand the result to the run, which then skips both checks.
        """
        self.check_media_urls(messages)
        self.check_model_choice(config)
        return RunChecks(input_checked=True)

    def _reject_tool_start_node(self, initial_state: dict[str, Any]) -> None:
        """Allow a run to start on any node except a ``ToolNode``.

        Clients may pick the start node, e.g. to jump straight to one agent. A ``ToolNode``
        runs the tool calls in the last message of the saved context without asking the
        model, so starting there would execute tools the model never requested.
        """
        node_name = initial_state.get("current_node")
        if node_name is None:
            return
        if not isinstance(node_name, str):
            raise ClientInputError("current_node must be a node name")
        nodes = self._nodes()
        # START and END ("__start__", "__end__") are nodes too.
        if nodes and node_name not in nodes:
            raise ClientInputError(f"unknown node '{node_name}'")
        node = nodes.get(node_name)
        if node is not None and isinstance(getattr(node, "func", None), ToolNode):
            raise ClientInputError(
                f"current_node '{node_name}' is a tool node; runs cannot start there"
            )

    def _validate_state_types(self, initial_state: dict[str, Any]) -> dict[str, Any]:
        """Check client state values against the state model's field types.

        Core stores a value on the state as given, so a wrong type (a string for an ``int``
        field) is saved and crashes a later run. Fields the model does not declare are ignored
        by core and pass. Returns the state with the validated (coerced) values.
        """
        state_graph = getattr(self._graph, "_state_graph", None)
        fields = getattr(type(getattr(state_graph, "_state", None)), "model_fields", None) or {}
        checked = dict(initial_state)
        for name, value in initial_state.items():
            field = fields.get(name)
            if field is None or field.annotation is None:
                continue
            try:
                checked[name] = TypeAdapter(field.annotation).validate_python(value)
            except ValidationError as exc:
                reason = exc.errors()[0]["msg"] if exc.errors() else "invalid value"
                raise ClientInputError(f"initial_state.{name} is invalid: {reason}") from exc
        return checked

    def _nodes(self) -> dict[str, Any]:
        """The compiled graph's nodes by name (empty when there is no state graph)."""
        state_graph = getattr(self._graph, "_state_graph", None)
        return getattr(state_graph, "nodes", {}) if state_graph else {}

    def tool_nodes(self) -> Iterator[tuple[str, ToolNode]]:
        """Every ``ToolNode`` in the graph with its node name."""
        for name, node in self._nodes().items():
            tool_node = getattr(node, "func", None)
            if isinstance(tool_node, ToolNode):
                yield name, tool_node

    def _remote_tool_names(self) -> set[str]:
        """Names of the tools the client runs (remote tools), across every ToolNode."""
        names: set[str] = set()
        for _, tool_node in self.tool_nodes():
            names.update(n for n in getattr(tool_node, "remote_tool_names", None) or [] if n)
        return names

    def _server_tool_names(self) -> set[str]:
        """Names of the tools the server runs itself (local and MCP), across every ToolNode."""
        names: set[str] = set()
        for _, tool_node in self.tool_nodes():
            names.update(getattr(tool_node, "_funcs", None) or {})
            names.update(getattr(tool_node, "mcp_tools", None) or [])
        return names

    async def _run_state(self, config: dict[str, Any], checks: RunChecks | None) -> Any:
        """The thread's state, read once per run and kept on ``checks`` for the other checks."""
        checks = checks or RunChecks()
        if checks.state is _UNREAD:
            checks.state = await self.checkpointer.aget_state(config) if self.checkpointer else None
        return checks.state

    async def _check_tool_results(
        self, messages: list[Any], config: dict[str, Any], checks: RunChecks | None = None
    ) -> None:
        """Accept a tool result only as the answer to a remote tool call awaiting one.

        Remote tools run on the client, which sends their results back to resume the run.
        Anything else is a forged result: an answer to a call the model never made, a second
        answer to the same call, or an answer to a server tool the client never ran.
        """
        result_ids = tool_result_ids(messages)
        if not result_ids:
            return
        if len(set(result_ids)) != len(result_ids):
            raise ClientInputError("Each tool call can be answered only once")

        state = await self._run_state(config, checks)
        pending = unanswered_tool_calls(getattr(state, "context", None))
        # A client tool can never carry a server tool's name: otherwise a client could declare
        # it and post a "result" for a server tool call (one paused for approval, say).
        remote = self._remote_tool_names() | (_run_tool_names(config) - self._server_tool_names())
        for call_id in result_ids:
            if call_id not in pending or pending[call_id] not in remote:
                raise ClientInputError(f"No remote tool call '{call_id}' is waiting for a result")

    def runs_policy(self) -> RunsConfig:
        """The ``runs`` policy from 10xgraph.json (defaults when the config has none)."""
        runs = getattr(self.config, "runs", None)
        return runs if isinstance(runs, RunsConfig) else RunsConfig()

    async def require_run_thread(self, thread_id: str | None, user: Identity | None) -> None:
        """Raise ``ThreadNotFoundError`` (404) unless ``user`` may run on ``thread_id``.

        Every run entry point calls this once, before the thread lock, the run slot and any
        response header. Runs stay in the caller's own threads whatever their reach; an
        unknown id is refused under ``runs.thread_ids: "server"``. ``None`` (the request
        starts a new thread) needs no check.
        """
        if thread_id is None:
            return
        await require_thread(
            self.checkpointer,
            user,
            thread_id,
            run=True,
            server_thread_ids=self.runs_policy().server_thread_ids,
        )

    async def new_thread_id(self) -> str:
        """A fresh server-made thread id (public name of :meth:`_new_thread_id`)."""
        return await self._new_thread_id()

    async def _new_thread_id(self) -> str:
        """Generate a thread id that nobody owns yet.

        The request asked for a new thread, so the id handed out here must really be new.
        Generated ids (Snowflake by default) are predictable, and with ``runs.thread_ids:
        "client"`` a client may create a thread under any id it likes, so another user could
        have claimed the next id first. Storage would refuse the run on it (404); picking an
        unused id avoids that.
        """
        for _ in range(MAX_THREAD_ID_ATTEMPTS):
            thread_id = str(await InjectQ.get_instance().atry_get("generated_id") or uuid4())
            try:
                owner = await self.checkpointer.aget_thread_owner(thread_id)
            except NotImplementedError:
                return thread_id  # the checkpointer cannot tell; keep the old behaviour
            if owner is None:
                return thread_id
            logger.warning("Generated thread id %s is already taken; generating another", thread_id)
        raise RuntimeError("Could not generate an unused thread id")

    async def _check_resume_target(
        self,
        graph_input: GraphInputSchema,
        config: dict[str, Any],
        checks: RunChecks | None = None,
    ) -> None:
        """Refuse a resume that names an interrupt the thread is not paused at.

        Only when the client sent ``interrupt_id``; without it the resume answers whatever the
        thread is paused at, as before.

        Raises:
            InterruptConflictError: The thread is not paused, or is paused at another interrupt.
        """
        if not graph_input.is_resume or graph_input.interrupt_id is None:
            return
        state = await self._run_state(config, checks)
        pending = pending_interrupt(state) if state is not None else None
        if pending is None:
            raise InterruptConflictError(
                "Nothing to resume: this thread is not paused at an interrupt"
            )
        if pending.id != graph_input.interrupt_id:
            raise InterruptConflictError(
                f"interrupt_id '{graph_input.interrupt_id}' does not match the open "
                f"interrupt '{pending.id}'"
            )

    async def check_resume_target(
        self,
        graph_input: GraphInputSchema,
        user: Identity | None,
        checks: RunChecks | None = None,
    ) -> None:
        """409 for a resume aimed at the wrong interrupt, before a stream response starts.

        A stream answers 200 as soon as it starts, so the router asks this first (under the
        thread lock), as invoke gets the same check inside its run. Nothing happens unless the
        request is a resume that names an ``interrupt_id``. The state read is kept on
        ``checks``, so the run reads it no second time.

        Raises:
            HTTPException: 409 when the thread is not paused at that interrupt.
        """
        if not graph_input.is_resume or graph_input.interrupt_id is None:
            return
        thread_id = normalize_thread_id((graph_input.config or {}).get("thread_id"))
        config: dict[str, Any] = {"thread_id": thread_id}
        set_user(config, user)
        try:
            if thread_id is None:  # a new thread has nothing to resume
                raise InterruptConflictError(
                    "Nothing to resume: this thread is not paused at an interrupt"
                )
            await self._check_resume_target(graph_input, config, checks)
        except _resume_error_types() as exc:
            raise _resume_http_error(exc) from exc

    async def _is_static_pause(self, config: dict[str, Any], checks: RunChecks) -> bool:
        """Whether the thread is paused by ``interrupt_before`` / ``interrupt_after``.

        Such a pause has nothing to answer: core continues it from any input that carries no
        ``resume`` key, so an empty resume must not reach core (it would fail with "Nothing to
        resume"). A pause on an ``interrupt()`` or on a client-side tool call is not static.
        """
        state = await self._run_state(config, checks)
        if state is None or not state.is_interrupted():
            return False
        pending = pending_interrupt(state)
        reason = getattr(state.execution_meta, "interrupt_reason", None)
        return pending is None and reason != "remote_tool_call"

    async def _prepare_input(
        self,
        graph_input: GraphInputSchema,
        user: Identity | None = None,
        server_config: dict[str, Any] | None = None,
        credential: UserCredential | None = None,
        checks: RunChecks | None = None,
    ):
        checks = checks or RunChecks()
        if not checks.input_checked:
            # External media URLs and the model name first: cheap, and no network.
            self.check_run_input(graph_input.messages, graph_input.config)
        is_new_thread = False
        config = client_config(graph_input.config)
        runs = self.runs_policy()
        reject_unlisted_keys(config, runs.config_keys, "config", ALWAYS_ALLOWED_CONFIG_KEYS)
        reject_builtin_state_keys(graph_input.initial_state, "initial_state")
        reject_unlisted_keys(
            graph_input.initial_state,
            runs.state_keys,
            "initial_state",
            setting="runs.state_keys",
        )
        # Server-owned keys set by a trusted caller (a protocol adapter), after the client's
        # own config has been stripped of them.
        config.update(server_config or {})
        # Same normalisation as the router's thread check, so the checked thread is the one used.
        thread_id = normalize_thread_id(config.get("thread_id"))
        if thread_id is None:
            thread_id = await self._new_thread_id()
            is_new_thread = True

        # update thread id
        config["thread_id"] = str(thread_id)

        # The authenticated user, before any checkpointer read below: PgCheckpointer refuses a
        # config without ``user_id``. Never taken from the request (``client_config`` strips it).
        # Storage reads the identity (tenant values, reach) from ``config["user"]``.
        set_user(config, user, credential)

        # check recursion limit set or not
        config["recursion_limit"] = graph_input.recursion_limit or 25

        # Server-owned cap on parallel tool calls per model turn (clients cannot set it)
        if runs.max_parallel_tool_calls is not None:
            config["max_parallel_tool_calls"] = runs.max_parallel_tool_calls

        # Preprocess multimodal messages (resolve file_id → cached text, etc.)
        # ``user`` (the authenticated identity) carries the tenant values files are scoped by.
        preprocessed = await preprocess_multimodal_messages(
            graph_input.messages,
            self.media_service,
            user.user_id if user else None,
            user=user,
        )
        # The accepted messages now use these files: record the thread on each (owned files
        # only; the router already checked the thread is the caller's).
        if user is not None and self.media_service is not None:
            keys = [k for m in preprocessed for k in internal_media_keys(m)]
            if keys:
                await self.media_service.link_threads(keys, str(thread_id), user.user_id, user)
        input_data: dict = {
            "messages": preprocessed,
        }
        if graph_input.is_resume and not await self._is_static_pause(config, checks):
            input_data["resume"] = graph_input.resume
            if graph_input.interrupt_id is not None:
                # Read by core 1.0+ (checks the id again and validates the answer); an older
                # core ignores unknown input keys.
                input_data["resume_interrupt_id"] = graph_input.interrupt_id
        if graph_input.initial_state:
            self._reject_tool_start_node(graph_input.initial_state)
            input_data["state"] = self._validate_state_types(graph_input.initial_state)

        return (
            input_data,
            config,
            {
                "is_new_thread": is_new_thread,
                "thread_id": str(thread_id),
            },
        )
