"""``@task``: declare a task, validate its kwargs and result from the type hints."""

from __future__ import annotations

import importlib
import inspect
import json
import sys
import typing
from collections.abc import Callable
from datetime import datetime
from typing import Any, NotRequired, TypedDict

from injectq import Inject
from pydantic import ConfigDict, TypeAdapter, ValidationError

from tenxgraph_api.jobs.errors import PayloadTooLarge, TaskDefinitionError, TaskValidationError

from . import registry
from .backoff import BackoffSpec, to_backoff
from .context import TaskContext
from .models import PRIORITIES, KeyMode, TaskRun


if typing.TYPE_CHECKING:
    from .client import RunHandle


EXECUTORS = ("async", "thread", "process")
CATCHUP = ("latest", "none")

ConcurrencyKey = str | Callable[[dict[str, Any], dict[str, Any]], Any]
RunHook = Callable[[TaskRun], Any]


def json_size(value: Any) -> int:
    """Bytes of the compact JSON encoding."""
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())


def _is_context(annotation: Any) -> bool:
    if annotation is TaskContext:
        return True
    return any(arg is TaskContext for arg in typing.get_args(annotation))


def _is_inject_default(default: Any) -> bool:
    """An InjectQ ``Inject[...]`` default (a lazy proxy, resolved when used). ``type()`` is
    used because the proxy's ``__class__`` resolves the dependency."""
    return type(default) is Inject


def _check_importable(fn: Callable[..., Any], where: str) -> None:
    """A ``process`` task runs in a child that imports it as ``module:qualname``.

    The module attribute is not bound yet while ``@task`` runs (the decorator runs first), so
    the "same object" check here only catches a name already bound to something else; the
    process executor resolves ``module:qualname`` again before the first run.
    """
    hint = "define it at module level in an importable module"
    if not inspect.isfunction(fn):
        raise TaskDefinitionError(
            f"{where}: executor='process' needs a plain module-level function, got "
            f"{type(fn).__name__}; {hint}"
        )
    qualname = fn.__qualname__
    if "<" in qualname:  # <lambda>, <locals>
        raise TaskDefinitionError(
            f"{where}: executor='process' cannot use {qualname!r} (a lambda or a function "
            f"defined inside another function): the child imports it by name; {hint}"
        )
    if fn.__module__ in (None, "__main__", "__mp_main__"):
        raise TaskDefinitionError(
            f"{where}: executor='process' cannot use a function from {fn.__module__!r}: the "
            f"child cannot import the main script; {hint}"
        )
    module = sys.modules.get(fn.__module__)
    if module is None:
        try:
            module = importlib.import_module(fn.__module__)
        except Exception as exc:
            raise TaskDefinitionError(
                f"{where}: executor='process' needs an importable module; importing "
                f"{fn.__module__!r} failed: {exc}"
            ) from exc
    current: Any = module
    for part in qualname.split("."):
        current = getattr(current, part, _MISSING)
        if current is _MISSING:
            return  # still being defined: checked again by the process executor
    if current is fn or getattr(current, "fn", None) is fn:
        return
    raise TaskDefinitionError(
        f"{where}: executor='process' needs {fn.__module__}:{qualname} to name this "
        f"function, but it names {type(current).__name__}; {hint}"
    )


_MISSING = object()


class Task:
    """A registered task. Calling it runs the function directly (no queue)."""

    def __init__(  # noqa: PLR0913 - mirrors the @task options
        self,
        fn: Callable[..., Any],
        *,
        name: str | None = None,
        queue: str = "default",
        priority: str = "normal",
        executor: str = "async",
        retries: int = 0,
        backoff: BackoffSpec = None,
        timeout: float | None = None,
        http: bool = False,
        schedule: str | list[str] | None = None,
        timezone: str = "UTC",
        identity: str | None = None,
        catchup: str = "latest",
        concurrency: int | None = None,
        concurrency_key: ConcurrencyKey | None = None,
        max_lost: int = 2,
        retention: float | None = None,
        on_success: RunHook | None = None,
        on_failure: RunHook | None = None,
    ) -> None:
        self.fn = fn
        self.name = name if name is not None else fn.__name__
        self.qualname = f"{fn.__module__}.{fn.__qualname__}"
        self.queue = queue
        self.priority = priority
        self.executor = executor
        self.retries = retries
        try:
            self.backoff = to_backoff(backoff)
        except (TypeError, ValueError) as exc:
            raise TaskDefinitionError(f"task {self.name!r}: {exc}") from exc
        self.timeout = timeout
        self.http = http
        self.schedule = schedule
        self.timezone = timezone
        self.identity = identity
        self.catchup = catchup
        self.concurrency = concurrency
        self.concurrency_key = concurrency_key
        self.max_lost = max_lost
        self.retention = retention
        self.on_success = on_success
        self.on_failure = on_failure
        self._check_options()
        self.context_param: str | None = None
        self._kwargs_adapter, self._result_adapter, self._params = self._build_adapters()
        if isinstance(concurrency_key, str) and concurrency_key not in self._params:
            raise TaskDefinitionError(
                f"task {self.name!r}: concurrency_key {concurrency_key!r} is not a parameter"
            )
        self.__name__ = fn.__name__
        self.__qualname__ = fn.__qualname__
        self.__module__ = fn.__module__
        self.__doc__ = fn.__doc__
        self.__wrapped__ = fn

    # ------------------------------------------------------------------ declaration checks

    def _check_options(self) -> None:  # noqa: PLR0912 - one check per option
        where = f"task {self.name!r}"
        if not isinstance(self.name, str) or not self.name.strip():
            raise TaskDefinitionError("task name must be a non-empty string")
        if not isinstance(self.queue, str) or not self.queue.strip():
            raise TaskDefinitionError(f"{where}: queue must be a non-empty string")
        if self.priority not in PRIORITIES:
            raise TaskDefinitionError(f"{where}: priority must be one of {PRIORITIES}")
        if self.executor not in EXECUTORS:
            raise TaskDefinitionError(f"{where}: executor must be one of {EXECUTORS}")
        self._check_executor(where)
        if not isinstance(self.retries, int) or self.retries < 0:
            raise TaskDefinitionError(f"{where}: retries must be an int >= 0")
        if self.timeout is not None and not self.timeout > 0:
            raise TaskDefinitionError(f"{where}: timeout must be > 0 seconds")
        if self.concurrency is not None and (
            not isinstance(self.concurrency, int) or self.concurrency < 1
        ):
            raise TaskDefinitionError(f"{where}: concurrency must be an int >= 1")
        if self.concurrency_key is not None and self.concurrency is None:
            raise TaskDefinitionError(f"{where}: concurrency_key needs concurrency")
        if not isinstance(self.max_lost, int) or self.max_lost < 0:
            raise TaskDefinitionError(f"{where}: max_lost must be an int >= 0")
        if self.retention is not None and self.retention < 0:
            raise TaskDefinitionError(f"{where}: retention must be >= 0 seconds")
        if self.catchup not in CATCHUP:
            raise TaskDefinitionError(f"{where}: catchup must be one of {CATCHUP}")
        if self.schedule is not None and not self.identity:
            raise TaskDefinitionError(f"{where}: schedule needs identity=")
        for hook_name in ("on_success", "on_failure"):
            hook = getattr(self, hook_name)
            if hook is not None and not callable(hook):
                raise TaskDefinitionError(f"{where}: {hook_name} must be callable")

    def _check_executor(self, where: str) -> None:
        fn = self.fn
        is_async = inspect.iscoroutinefunction(fn) or inspect.isasyncgenfunction(fn)
        if self.executor == "async":
            if not inspect.iscoroutinefunction(fn):
                raise TaskDefinitionError(
                    f"{where}: executor='async' needs an `async def` function"
                )
            return
        if is_async:
            raise TaskDefinitionError(
                f"{where}: executor={self.executor!r} needs a plain `def` function, not "
                "`async def` (use executor='async' for async code)"
            )
        if self.executor == "process":
            _check_importable(fn, where)

    def _build_adapters(self) -> tuple[TypeAdapter, TypeAdapter, set[str]]:
        where = f"task {self.name!r}"
        signature = inspect.signature(self.fn)
        try:
            hints = typing.get_type_hints(self.fn, include_extras=True)
        except Exception:  # unresolvable forward references: fall back to raw annotations
            hints = {}
        fields: dict[str, Any] = {}
        for param in signature.parameters.values():
            annotation = hints.get(param.name, param.annotation)
            if annotation is inspect.Parameter.empty:
                annotation = Any
            if _is_context(annotation):
                self.context_param = param.name
                continue
            if _is_inject_default(param.default):  # resolved from the container, not kwargs
                if self.executor == "process":
                    raise TaskDefinitionError(
                        f"{where}: executor='process' cannot use Inject[...] ({param.name}): "
                        "the child does not load the container; pass plain arguments"
                    )
                continue
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                raise TaskDefinitionError(f"{where}: *args and **kwargs are not supported")
            if param.kind is param.POSITIONAL_ONLY:
                raise TaskDefinitionError(f"{where}: positional-only parameters are not supported")
            fields[param.name] = (
                annotation if param.default is inspect.Parameter.empty else NotRequired[annotation]
            )
        try:
            make_typed_dict: Any = TypedDict  # built at run time from the signature
            kwargs_type: Any = make_typed_dict(f"{self.name}_kwargs", fields)
            kwargs_type.__pydantic_config__ = ConfigDict(extra="forbid")
            kwargs_adapter = TypeAdapter(kwargs_type)
            returns = hints.get("return", signature.return_annotation)
            if returns is inspect.Signature.empty:
                returns = Any
            result_adapter = TypeAdapter(returns)
        except Exception as exc:
            raise TaskDefinitionError(f"{where}: type hints are not usable: {exc}") from exc
        return kwargs_adapter, result_adapter, set(fields)

    # ------------------------------------------------------------------ validation

    def validate_kwargs(self, kwargs: dict[str, Any], *, max_bytes: int) -> dict[str, Any]:
        """Validated kwargs in JSON form (what the store keeps). Raises
        ``TaskValidationError`` / ``PayloadTooLarge``."""
        try:
            validated = self._kwargs_adapter.validate_python(kwargs)
            data = self._kwargs_adapter.dump_python(validated, mode="json")
        except ValidationError as exc:
            raise TaskValidationError(
                f"invalid kwargs for task {self.name!r}: {exc}",
                errors=json.loads(exc.json()),
            ) from None
        except Exception as exc:
            raise TaskValidationError(
                f"kwargs for task {self.name!r} are not JSON-serializable: {exc}"
            ) from None
        size = json_size(data)
        if size > max_bytes:
            raise PayloadTooLarge(
                f"kwargs for task {self.name!r} are {size} bytes (limit {max_bytes})"
            )
        return data

    def load_kwargs(self, stored: dict[str, Any]) -> dict[str, Any]:
        """Stored JSON kwargs back to Python values, validated again (worker side)."""
        try:
            return dict(self._kwargs_adapter.validate_python(stored))
        except ValidationError as exc:
            raise TaskValidationError(
                f"stored kwargs no longer match task {self.name!r}: {exc}",
                errors=json.loads(exc.json()),
            ) from None

    def dump_result(self, value: Any, *, max_bytes: int) -> Any:
        """The result in JSON form, validated against the return annotation."""
        try:
            validated = self._result_adapter.validate_python(value)
            data = self._result_adapter.dump_python(validated, mode="json")
        except ValidationError as exc:
            raise TaskValidationError(
                f"result of task {self.name!r} does not match its return type: {exc}",
                errors=json.loads(exc.json()),
            ) from None
        except Exception as exc:
            raise TaskValidationError(
                f"result of task {self.name!r} is not JSON-serializable: {exc}"
            ) from None
        size = json_size(data)
        if size > max_bytes:
            raise PayloadTooLarge(
                f"result of task {self.name!r} is {size} bytes (limit {max_bytes})"
            )
        return data

    def kwargs_schema(self) -> dict[str, Any]:
        """JSON schema of the kwargs (for ``GET /v1/tasks`` and the playground form)."""
        return self._kwargs_adapter.json_schema()

    def concurrency_key_for(self, kwargs: dict[str, Any], identity: dict[str, Any]) -> str | None:
        if self.concurrency is None or self.concurrency_key is None:
            return None
        if isinstance(self.concurrency_key, str):
            value = kwargs.get(self.concurrency_key)
        else:
            value = self.concurrency_key(kwargs, identity)
        return None if value is None else str(value)

    # ------------------------------------------------------------------ enqueue

    def options(
        self,
        *,
        identity: Any = None,
        key: str | None = None,
        key_mode: KeyMode = "keep",
        priority: str | None = None,
        delay: float | None = None,
        at: datetime | float | None = None,
        trigger: dict[str, Any] | None = None,
    ) -> TaskCall:
        """Run options, kept apart from task arguments (a task may have a ``key`` parameter)."""
        return TaskCall(
            self,
            identity=identity,
            key=key,
            key_mode=key_mode,
            priority=priority,
            delay=delay,
            at=at,
            trigger=trigger,
        )

    async def enqueue(self, **kwargs: Any) -> RunHandle:
        """Enqueue with default options (identity inherited from the running task)."""
        return await TaskCall(self).enqueue(**kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.fn(*args, **kwargs)

    def __repr__(self) -> str:
        return f"<Task {self.name!r} queue={self.queue!r}>"


class TaskCall:
    """A task with run options bound; ``.enqueue(**kwargs)`` writes the run."""

    def __init__(
        self,
        task: Task,
        *,
        identity: Any = None,
        key: str | None = None,
        key_mode: KeyMode = "keep",
        priority: str | None = None,
        delay: float | None = None,
        at: datetime | float | None = None,
        trigger: dict[str, Any] | None = None,
    ) -> None:
        if key_mode not in ("keep", "replace"):
            raise ValueError("key_mode must be 'keep' or 'replace'")
        if priority is not None and priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}")
        if delay is not None and at is not None:
            raise ValueError("pass delay= or at=, not both")
        if delay is not None and delay < 0:
            raise ValueError("delay must be >= 0")
        if key is not None and (not isinstance(key, str) or not key):
            raise ValueError("key must be a non-empty string")
        self.task = task
        self.identity = identity
        self.key = key
        self.key_mode: KeyMode = key_mode
        self.priority = priority
        self.delay = delay
        self.at = at
        self.trigger = trigger

    async def enqueue(self, **kwargs: Any) -> RunHandle:
        from .client import enqueue_call

        return await enqueue_call(self, kwargs)


def task(  # noqa: PLR0913 - mirrors the documented options
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    queue: str = "default",
    priority: str = "normal",
    executor: str = "async",
    retries: int = 0,
    backoff: BackoffSpec = None,
    timeout: float | None = None,
    http: bool = False,
    schedule: str | list[str] | None = None,
    timezone: str = "UTC",
    identity: str | None = None,
    catchup: str = "latest",
    concurrency: int | None = None,
    concurrency_key: ConcurrencyKey | None = None,
    max_lost: int = 2,
    retention: float | None = None,
    on_success: RunHook | None = None,
    on_failure: RunHook | None = None,
) -> Any:
    """Declare a task. Use as ``@task`` or ``@task(...)``; see JOBS_PLAN.md for each option.

    Raises ``DuplicateTaskName`` when the name is taken and ``TaskDefinitionError`` for an
    invalid option or signature. ``schedule`` and ``http`` are recorded but act only once the
    scheduler (Sprint 4) and routes (Sprint 5) exist.
    """

    def decorate(func: Callable[..., Any]) -> Task:
        obj = Task(
            func,
            name=name,
            queue=queue,
            priority=priority,
            executor=executor,
            retries=retries,
            backoff=backoff,
            timeout=timeout,
            http=http,
            schedule=schedule,
            timezone=timezone,
            identity=identity,
            catchup=catchup,
            concurrency=concurrency,
            concurrency_key=concurrency_key,
            max_lost=max_lost,
            retention=retention,
            on_success=on_success,
            on_failure=on_failure,
        )
        registry.register(obj)
        return obj

    if fn is not None:
        return decorate(fn)
    return decorate
