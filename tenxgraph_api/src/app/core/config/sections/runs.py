"""``runs`` section of 10xgraph.json."""

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    pass


logger = logging.getLogger("tenxgraph_api")


# Finite default so one account cannot hold an unbounded number of HTTP graph runs open.
DEFAULT_MAX_CONCURRENT_RUNS_PER_USER = 10
THREAD_ID_MODES = ("client", "server")

# Longest a graph run may take when MODE=production and ``runs.timeout`` is not set.
DEFAULT_PRODUCTION_RUN_TIMEOUT_SECONDS = 300.0


def default_run_timeout() -> float | None:
    """``runs.timeout`` when the config leaves it unset: 300 s in production, else no limit.

    MODE is read from the environment (as the settings validators do). An invalid MODE is
    reported by ``Settings`` itself, so here it counts as development.
    """
    from tenxgraph_api.src.app.core.config.settings import normalize_mode_value

    try:
        mode = normalize_mode_value(os.environ.get("MODE"))
    except ValueError:
        return None
    return DEFAULT_PRODUCTION_RUN_TIMEOUT_SECONDS if mode == "production" else None


@dataclass
class RunsConfig:
    """Per-run policy for graph runs (``runs`` key in 10xgraph.json).

    Example::

        "runs": {
            "thread_ids": "server",
            "config_keys": ["locale", "tenant_hint"],
            "state_keys": ["preferences"],
            "max_concurrent_per_user": 10,
            "timeout": 600,
            "max_parallel_tool_calls": 10
        }

    ``thread_ids`` decides who may name a new thread, with auth on or off. ``"server"`` (the
    default) accepts only ids of threads that already exist; a new thread is started by
    omitting the id (or sending ``"new"`` over WebSocket), and the server picks it.
    ``"client"`` lets a request start a thread under any id it picks; the first writer of an id
    then owns it, so a user who claims a guessable id (``"default"``) first locks its intended
    owner out. A thread the caller cannot see and an unknown one both answer 404. AG-UI
    (``POST /v1/ag-ui``) and realtime (``/v1/graph/live``) follow the same rule as REST: with
    ``"server"`` an id the server has never seen is refused (404). An AG-UI client that has no
    thread yet sends ``threadId`` blank or ``"new"`` and reads the server's id from
    ``RUN_STARTED``; a frontend that makes its own ids needs ``"client"``.

    ``config_keys`` and ``state_keys`` are allow-lists for client input. A request whose
    ``config`` carries a key outside ``config_keys`` is rejected with 422; ``thread_id`` and
    ``run_id`` are always allowed, and ``null`` (the default) accepts any key except the
    server-owned ones.

    ``state_keys`` denies by default, like ``remote_tools``: only the listed keys may be set by
    a client in ``initial_state``, PUT state and AG-UI ``state``. Unset, ``null`` or ``[]``
    allows none, and any other key is rejected with 422. ``context`` in PUT state is always
    allowed (it has its own message rules). List only keys a client may be trusted with: a
    graph tool or node that trusts a value such as ``role`` or ``plan`` from state must not
    list it.

    ``max_concurrent_per_user`` caps the HTTP graph runs (invoke, stream, AG-UI) one verified
    user may have in flight at once in this process; over the cap the request gets 429.
    Default 10; ``0`` or ``null`` means unlimited. Requests without a verified user (auth off)
    are not capped. ``timeout`` is the longest a single graph run may take, in seconds; a run
    that passes it is cancelled (invoke answers 504; a stream, ``/v1/graph/ws`` run or AG-UI
    run ends with an error). Realtime sessions are not limited. When the key is absent the
    default is 300 seconds with MODE=production and no limit in development; an explicit
    number overrides it, and an explicit ``null`` means no limit in either mode.

    ``max_parallel_tool_calls`` caps the tool calls one model turn may run at once. The API
    puts it into every run's config (clients cannot set it). Default ``null``: core decides,
    from the ``TENXGRAPH_MAX_PARALLEL_TOOL_CALLS`` environment variable, else 10.
    """

    thread_ids: str = "server"
    config_keys: frozenset[str] | None = None
    state_keys: frozenset[str] = frozenset()
    max_concurrent_per_user: int | None = DEFAULT_MAX_CONCURRENT_RUNS_PER_USER
    timeout: float | None = field(default_factory=default_run_timeout)
    max_parallel_tool_calls: int | None = None

    @property
    def server_thread_ids(self) -> bool:
        return self.thread_ids == "server"

    @classmethod
    def from_dict(cls, data: dict) -> "RunsConfig":
        if not isinstance(data, dict):
            raise ValueError("runs must be an object")
        thread_ids = data.get("thread_ids", "server")
        if thread_ids not in THREAD_ID_MODES:
            raise ValueError(f"runs.thread_ids must be one of {list(THREAD_ID_MODES)}")
        return cls(
            thread_ids=thread_ids,
            config_keys=_key_allow_list(data, "config_keys"),
            state_keys=_key_allow_list(data, "state_keys") or frozenset(),
            max_concurrent_per_user=_runs_limit(data),
            timeout=_runs_timeout(data),
            max_parallel_tool_calls=_runs_max_parallel_tool_calls(data),
        )


def _key_allow_list(data: dict, key: str) -> frozenset[str] | None:
    raw = data.get(key)
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"runs.{key} must be a list of strings or null")
    return frozenset(item.strip() for item in raw if item.strip())


def _runs_limit(data: dict) -> int | None:
    key = "max_concurrent_per_user"
    if key not in data:
        return DEFAULT_MAX_CONCURRENT_RUNS_PER_USER
    raw = data[key]
    if raw in (None, 0):
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError(f"runs.{key} must be a non-negative integer or null")
    return raw


def _runs_timeout(data: dict) -> float | None:
    if "timeout" not in data:
        return default_run_timeout()  # unset: mode default. An explicit null is no limit.
    raw = data["timeout"]
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float) or raw <= 0:
        raise ValueError("runs.timeout must be a positive number of seconds or null")
    return float(raw)


def _runs_max_parallel_tool_calls(data: dict) -> int | None:
    raw = data.get("max_parallel_tool_calls")
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ValueError("runs.max_parallel_tool_calls must be a positive integer or null")
    return raw
