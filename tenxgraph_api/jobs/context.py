"""``TaskContext``: what a running task can see and do."""

from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from typing import Any

from .emit import TaskEmitter


logger = logging.getLogger("tenxgraph_api.jobs")

#: Namespace for ``thread_id()``; fixed forever so ids stay stable across releases.
THREAD_NAMESPACE = uuid.UUID("5b0f8e8e-6d44-4b8e-9f0e-10a9a7a0c0de")

#: Largest thread id for a checkpointer with numeric ids (``PgCheckpointer(id_type=...)``).
NUMERIC_ID_MAX = {"int": 2**31 - 1, "bigint": 2**63 - 1}

#: Config keys ``invoke_graph`` always sets itself; a task's ``config`` cannot change them.
GRAPH_RUN_KEYS = ("thread_id", "run_id", "user", "user_id")


def numeric_id_type(graph: Any) -> str | None:
    """``"int"`` or ``"bigint"`` when ``graph``'s checkpointer stores numeric thread ids, else
    None. Only ``PgCheckpointer`` declares ``id_type``; other checkpointers take any id."""
    checkpointer = getattr(graph, "_checkpointer", None)
    raw = getattr(checkpointer, "id_type", None)
    value = str(getattr(raw, "value", raw)) if raw is not None else None
    return value if value in NUMERIC_ID_MAX else None


def derived_thread_id(run_id: str, id_type: str | None = None) -> str | int:
    """The graph thread id of ``run_id``, the same on every attempt and every pod.

    A UUID string (uuid5 of ``run_id``) by default. For a checkpointer with numeric ids
    (``id_type`` ``int`` or ``bigint``) it is a positive int taken from the SHA-256 of
    ``run_id``, in ``1 .. 2**31 - 1`` or ``1 .. 2**63 - 1``. A collision with another run's
    thread is possible in principle (about 1 in 2**31 per pair for ``int``); use a string or
    ``bigint`` id type when many runs share one checkpointer.
    """
    if id_type in NUMERIC_ID_MAX:
        top = NUMERIC_ID_MAX[id_type]
        digest = int.from_bytes(hashlib.sha256(run_id.encode()).digest()[:8], "big")
        return digest % top + 1
    return str(uuid.uuid5(THREAD_NAMESPACE, run_id))


class TaskContext:
    """Injected into a task by type (``ctx: TaskContext``); optional.

    ``run_id`` is a good idempotency key for external APIs: it is the same on every attempt.
    """

    def __init__(
        self,
        *,
        run_id: str,
        attempt: int,
        task: str,
        queue: str,
        identity: dict[str, Any],
        emit: TaskEmitter,
        graph: Any = None,
    ) -> None:
        self.run_id = run_id
        self.attempt = attempt
        self.task = task
        self.queue = queue
        self.identity = identity
        self.emit = emit
        self._graph = graph
        self._cancelled = threading.Event()

    @property
    def cancelled(self) -> bool:
        """True once a cancel was asked (or the attempt is being stopped). Check it in long
        loops; an ``async`` task is also cancelled with ``asyncio`` cancellation."""
        return self._cancelled.is_set()

    def _set_cancelled(self) -> None:
        self._cancelled.set()

    def thread_id(self) -> str | int:
        """Graph thread id for this run, the same on every attempt (see ``derived_thread_id``).

        A UUID string, or a stable positive int when the loaded graph's checkpointer uses
        numeric ids (``PgCheckpointer(id_type="int" | "bigint")``).
        """
        return derived_thread_id(self.run_id, numeric_id_type(self._graph))

    def graph_config(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """The run config ``invoke_graph`` passes to the graph (see there)."""
        cfg = {k: v for k, v in (config or {}).items() if k not in GRAPH_RUN_KEYS}
        user = dict(self.identity)
        cfg["thread_id"] = self.thread_id()
        cfg["run_id"] = f"{self.run_id}:{self.attempt}"
        cfg["user"] = user
        cfg["user_id"] = user.get("user_id") or "anonymous"
        cfg.setdefault("recursion_limit", 25)
        metadata = dict(cfg.get("metadata") or {})
        metadata.update(task_run_id=self.run_id, task=self.task, attempt=self.attempt)
        cfg["metadata"] = metadata
        return cfg

    async def invoke_graph(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any] | None = None,
        *,
        response_granularity: Any = None,
    ) -> Any:
        """Run the loaded graph (the ``agent`` of 10xgraph.json) as this run.

        - ``thread_id`` is ``ctx.thread_id()``, the same on every attempt, so a retried or
          recovered run continues from the checkpoint; the node that was running re-runs.
        - The graph ``run_id`` is ``"<run_id>:<attempt>"`` (unique per attempt, so core's
          dead-run repair applies to the attempt that died).
        - ``user`` / ``user_id`` come from the run identity, the same ``config["user"]`` shape
          the API uses, and the thread is saved with that user as its owner.
        - ``input_data`` is sent on attempt 1 only; later attempts send ``{"messages": []}``,
          because resending the input would duplicate the user message. Call this once per
          task; a second call in the same task is a second turn on the same thread.
        - ``metadata`` carries ``task_run_id``, ``task`` and ``attempt``.

        Keys in ``config`` other than ``thread_id``, ``run_id``, ``user`` and ``user_id``
        (always set here) are passed through, such as ``recursion_limit``.
        """
        graph = self._graph
        if graph is None:
            raise RuntimeError(
                "ctx.invoke_graph needs the loaded graph: it runs inside `10xgraph worker`, "
                "which loads the `agent` of 10xgraph.json (pass graph= to Worker in tests)"
            )
        cfg = self.graph_config(config)
        data = input_data if self.attempt <= 1 else {"messages": []}
        await self._save_thread(graph, cfg)
        kwargs: dict[str, Any] = {}
        if response_granularity is not None:
            kwargs["response_granularity"] = response_granularity
        return await graph.ainvoke(data, config=cfg, **kwargs)

    @staticmethod
    async def _save_thread(graph: Any, cfg: dict[str, Any]) -> None:
        """Record the thread with the run's user as owner, as the API does for every run, so
        the API's ownership checks can serve it. Best effort."""
        checkpointer = getattr(graph, "_checkpointer", None)
        if checkpointer is None or not hasattr(checkpointer, "aput_thread"):
            return
        try:
            from tenxgraph.utils.thread_info import ThreadInfo

            info = ThreadInfo(thread_id=cfg["thread_id"], user_id=cfg["user_id"])
            await checkpointer.aput_thread(cfg, info)
        except Exception:
            logger.warning("could not save thread %s for the task run", cfg["thread_id"])

    def __repr__(self) -> str:
        return f"TaskContext(run_id={self.run_id!r}, task={self.task!r}, attempt={self.attempt})"
