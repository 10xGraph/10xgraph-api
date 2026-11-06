"""@task declaration: options, registry, signature and payload validation."""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import BaseModel

from tenxgraph_api.jobs import (
    DuplicateTaskName,
    PayloadTooLarge,
    TaskContext,
    TaskDefinitionError,
    TaskNotFound,
    TaskValidationError,
    get_task,
    task,
)
from tenxgraph_api.jobs.backoff import Backoff, to_backoff
from tenxgraph_api.jobs.registry import all_tasks, isolated_registry


@pytest.fixture(autouse=True)
def _registry():
    with isolated_registry():
        yield


class Item(BaseModel):
    sku: str
    qty: int = 1


def test_bare_decorator_and_defaults():
    @task
    async def td_plain(x: int) -> int:
        return x

    assert get_task("td_plain") is td_plain
    assert td_plain.queue == "default"
    assert td_plain.priority == "normal"
    assert td_plain.retries == 0
    assert td_plain.max_lost == 2
    assert td_plain.context_param is None
    assert td_plain.__name__ == "td_plain"


@pytest.mark.asyncio
async def test_task_object_is_callable_directly():
    @task(name="td_direct")
    async def fn(x: int) -> int:
        return x * 2

    assert await fn(4) == 8


def test_name_option_and_duplicate_refused():
    @task(name="td_dup")
    async def first() -> None: ...

    with pytest.raises(DuplicateTaskName, match="td_dup"):

        @task(name="td_dup")
        async def second() -> None: ...

    assert all_tasks()["td_dup"] is first


def test_unknown_name():
    with pytest.raises(TaskNotFound):
        get_task("td_nope")


def test_context_injected_by_type_any_name():
    @task
    async def td_ctx(a: str, run_context: TaskContext) -> None: ...

    assert td_ctx.context_param == "run_context"
    schema = td_ctx.kwargs_schema()
    assert set(schema["properties"]) == {"a"}


def test_optional_context_annotation():
    @task
    async def td_ctx_opt(a: str, ctx: TaskContext | None = None) -> None: ...

    assert td_ctx_opt.context_param == "ctx"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"priority": "urgent"}, "priority"),
        ({"queue": ""}, "queue"),
        ({"executor": "gpu"}, "executor"),
        ({"retries": -1}, "retries"),
        ({"timeout": 0}, "timeout"),
        ({"concurrency": 0}, "concurrency"),
        ({"concurrency_key": "a"}, "concurrency_key needs concurrency"),
        ({"concurrency": 1, "concurrency_key": "missing"}, "not a parameter"),
        ({"max_lost": -1}, "max_lost"),
        ({"retention": -5}, "retention"),
        ({"catchup": "all"}, "catchup"),
        ({"schedule": "0 2 * * *"}, "identity"),
        ({"on_success": "nope"}, "on_success"),
        ({"backoff": "fast"}, "backoff"),
        ({"name": " "}, "name"),
    ],
)
def test_bad_options(kwargs, message):
    with pytest.raises(TaskDefinitionError, match=message):

        @task(**kwargs)
        async def td_bad(a: int) -> None: ...


def test_thread_and_process_executors_need_plain_def():
    @task(executor="thread")
    def td_thread() -> None: ...

    assert td_thread.executor == "thread"
    for executor in ("thread", "process"):
        with pytest.raises(TaskDefinitionError, match="plain `def`"):

            @task(name=f"td_async_{executor}", executor=executor)
            async def td_async() -> None: ...


def test_process_executor_needs_module_level_function():
    with pytest.raises(TaskDefinitionError, match="inside another function"):

        @task(executor="process")
        def td_local() -> None: ...

    with pytest.raises(TaskDefinitionError, match="lambda"):
        task(lambda: None, name="td_lambda", executor="process")


def test_process_executor_rejects_inject():
    from injectq import Inject

    from tests.unit_tests.jobs import process_tasks

    def td_inject(x: int, svc: str = Inject[str]) -> None: ...

    td_inject.__qualname__ = "td_inject"
    td_inject.__module__ = process_tasks.__name__
    with pytest.raises(TaskDefinitionError, match="Inject"):
        task(td_inject, executor="process")


def test_async_executor_needs_coroutine():
    with pytest.raises(TaskDefinitionError, match="async def"):

        @task
        def td_sync() -> None: ...


def test_var_args_refused():
    with pytest.raises(TaskDefinitionError, match="args"):

        @task
        async def td_var(**kw) -> None: ...


def test_positional_only_refused():
    with pytest.raises(TaskDefinitionError, match="positional-only"):

        @task
        async def td_pos(a: int, /) -> None: ...


def test_schedule_with_identity_is_recorded():
    @task(schedule="0 2 * * *", identity="nightly", timezone="Asia/Dhaka")
    async def td_cron() -> None: ...

    assert td_cron.schedule == "0 2 * * *"
    assert td_cron.timezone == "Asia/Dhaka"


def test_kwargs_validation_and_json_form():
    @task
    async def td_types(when: dt.datetime, item: Item, tags: list[str] | None = None) -> None: ...

    data = td_types.validate_kwargs(
        {"when": dt.datetime(2026, 11, 6, tzinfo=dt.UTC), "item": {"sku": "a"}},
        max_bytes=10_000,
    )
    assert data == {"when": "2026-11-06T00:00:00Z", "item": {"sku": "a", "qty": 1}}
    loaded = td_types.load_kwargs(data)
    assert isinstance(loaded["when"], dt.datetime)
    assert isinstance(loaded["item"], Item)

    with pytest.raises(TaskValidationError) as info:
        td_types.validate_kwargs({"when": "not a date", "item": {"sku": "a"}}, max_bytes=10_000)
    assert info.value.errors
    with pytest.raises(TaskValidationError):
        td_types.validate_kwargs({"item": {"sku": "a"}}, max_bytes=10_000)  # missing
    with pytest.raises(TaskValidationError):
        td_types.validate_kwargs(
            {"when": "2026-01-01T00:00:00Z", "item": {"sku": "a"}, "extra": 1},
            max_bytes=10_000,
        )


def test_kwargs_size_limit():
    @task
    async def td_big(blob: str) -> None: ...

    td_big.validate_kwargs({"blob": "x" * 100}, max_bytes=200)
    with pytest.raises(PayloadTooLarge, match="limit 200"):
        td_big.validate_kwargs({"blob": "x" * 300}, max_bytes=200)


def test_kwargs_not_json_serializable():
    @task
    async def td_any(value) -> None: ...

    with pytest.raises(TaskValidationError, match="JSON"):
        td_any.validate_kwargs({"value": object()}, max_bytes=10_000)


def test_result_validation():
    @task
    async def td_result() -> dict[str, int]:
        return {}

    assert td_result.dump_result({"a": 1}, max_bytes=100) == {"a": 1}
    with pytest.raises(TaskValidationError):
        td_result.dump_result({"a": "x"}, max_bytes=100)
    with pytest.raises(PayloadTooLarge):
        td_result.dump_result({str(i): i for i in range(100)}, max_bytes=100)


def test_unannotated_result_must_be_json():
    @task
    async def td_noann():
        return None

    assert td_noann.dump_result({"a": [1, 2]}, max_bytes=100) == {"a": [1, 2]}
    with pytest.raises(TaskValidationError):
        td_noann.dump_result(object(), max_bytes=100)


def test_concurrency_key_by_name_and_callable():
    @task(concurrency=2, concurrency_key="tenant")
    async def td_key_name(tenant: str) -> None: ...

    @task(concurrency=2, concurrency_key=lambda kw, ident: ident["user_id"])
    async def td_key_fn(tenant: str) -> None: ...

    assert td_key_name.concurrency_key_for({"tenant": "t1"}, {"user_id": "u"}) == "t1"
    assert td_key_fn.concurrency_key_for({"tenant": "t1"}, {"user_id": "u"}) == "u"


def test_options_validation():
    @task
    async def td_opts(key: str, delay: int) -> None: ...

    call = td_opts.options(identity={"user_id": "u"}, key="k", priority="high", delay=3)
    assert call.key == "k" and call.priority == "high"
    with pytest.raises(ValueError, match="key_mode"):
        td_opts.options(key_mode="merge")
    with pytest.raises(ValueError, match="priority"):
        td_opts.options(priority="urgent")
    with pytest.raises(ValueError, match="not both"):
        td_opts.options(delay=1, at=5.0)
    with pytest.raises(ValueError, match="delay"):
        td_opts.options(delay=-1)
    with pytest.raises(ValueError, match="key"):
        td_opts.options(key="")


def test_backoff():
    b = Backoff(base=2, cap=10, jitter=False)
    assert [b.delay(n) for n in (1, 2, 3, 4, 5)] == [2, 4, 8, 10, 10]
    assert b.delay(1000) == 10
    jittered = Backoff(base=4, cap=100)
    assert all(2 <= jittered.delay(1) <= 4 for _ in range(50))
    assert to_backoff(3)(7) == 3.0
    assert to_backoff(lambda n: n * 10)(2) == 20
    assert 1 <= to_backoff(None)(1) <= 2
    with pytest.raises(ValueError):
        to_backoff(-1)
    with pytest.raises(TypeError):
        to_backoff(True)
    with pytest.raises(ValueError):
        Backoff(base=-1)
