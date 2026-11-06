"""``10xgraph tasks``: enqueue, inspect, cancel and migrate background task runs.

Every action loads the project like ``10xgraph worker`` does (config, env file, ``sys.path``)
and needs a ``worker`` section in the config. Read actions (``runs``, ``run``, ``cancel``,
``migrate --check``) never create storage: a missing or outdated store answers "store not
initialised, run 10xgraph tasks migrate".
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tenxgraph_api.cli.commands import BaseCommand
from tenxgraph_api.cli.commands.worker import WorkerCommand
from tenxgraph_api.cli.constants import DEFAULT_CONFIG_FILE
from tenxgraph_api.cli.exceptions import CLIError, ConfigurationError, ValidationError


NOT_INITIALISED = "store not initialised, run 10xgraph tasks migrate"
PRIORITY_CHOICES = ("high", "normal", "low")
RECENT_EVENTS = 10


def _jobs_error(message: str, exit_code: int = 1) -> CLIError:
    return CLIError(message, exit_code, code="AF-JOBS-001")


def _when(value: float | None) -> str:
    if value is None:
        return "-"
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="seconds")


def _resolve(path: str, base_dir: Path) -> Path:
    target = Path(path).expanduser()
    return target if target.is_absolute() else base_dir / target


class TasksCommand(BaseCommand):
    """Run one ``10xgraph tasks`` action."""

    def execute(self, action: str, config: str = DEFAULT_CONFIG_FILE, **opts: Any) -> int:
        try:
            handler = getattr(self, f"_do_{action}")
            return int(handler(config, **opts))
        except Exception as e:
            return self.handle_error(e)

    # ------------------------------------------------------------------ helpers

    @property
    def _structured(self) -> bool:
        from tenxgraph_api.cli.capabilities import OutputFormat

        return self.output.capabilities.output_format in {OutputFormat.JSON, OutputFormat.JSONL}

    def _load(self, config: str) -> tuple[Any, Path]:
        """The worker config and the directory of the config file."""
        from tenxgraph_api.jobs.config.config import WorkerConfigError

        graph_config, config_path = WorkerCommand(self.output)._load_project(config)
        try:
            cfg = graph_config.worker
        except WorkerConfigError as exc:
            raise ConfigurationError(str(exc), config_path=str(config_path)) from exc
        if cfg is None:
            raise ConfigurationError(
                'No "worker" section in the config. Add one, for example "worker": '
                '{"modules": "jobs", "backend": "local"}',
                config_path=str(config_path),
            )
        if cfg.store_type == "memory" or cfg.transport_type == "memory":
            raise ConfigurationError(
                'worker "memory" backends work only inside one process; use "backend": "local"',
                config_path=str(config_path),
            )
        return cfg, config_path.parent

    def _sqlite_versions(self, path: Path) -> dict[str, int] | None:
        """Recorded schema versions of a jobs SQLite file, read-only; None when no file."""
        if not path.exists():
            return None
        from tenxgraph_api.jobs.store.sqlite import SCHEMA

        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        except sqlite3.Error:
            return {}
        try:
            rows = conn.execute(f"SELECT component, version FROM {SCHEMA}").fetchall()  # noqa: S608
        except sqlite3.Error:
            return {}
        finally:
            conn.close()
        return {str(name): int(version) for name, version in rows}

    def _preflight(self, cfg: Any, base_dir: Path) -> None:
        """Refuse a read when the store was never created or is older than the code. No DDL."""
        kind = cfg.store_type
        if kind == "sqlite":
            from tenxgraph_api.jobs.store.sqlite import STORE_MIGRATIONS

            versions = self._sqlite_versions(_resolve(cfg.store_path, base_dir))
            if (versions or {}).get("store", 0) < len(STORE_MIGRATIONS):
                raise _jobs_error(NOT_INITIALISED)
        elif kind == "postgres":
            from tenxgraph_api.jobs.store.postgres import CURRENT_SCHEMA_VERSION
            from tenxgraph_api.jobs.store.postgres import migrate as pg_migrate

            assert cfg.store is not None  # noqa: S101 - postgres has a store section
            version = asyncio.run(
                pg_migrate(cfg.store.expanded_url(), cfg.store.db_schema, check=True)
            )
            if version < CURRENT_SCHEMA_VERSION:
                raise _jobs_error(NOT_INITIALISED)

    async def _open(self, cfg: Any, base_dir: Path, *, full: bool) -> Any:
        """A runtime: with ``full`` the store and transport are set up (enqueue); otherwise only
        the store (the transport is built but never touched)."""
        from tenxgraph_api.jobs.config.config import WorkerConfigError
        from tenxgraph_api.jobs.config.factory import build_runtime

        try:
            runtime = await build_runtime(cfg, base_dir=base_dir, allow_memory=False, setup=full)
            if not full:
                try:
                    await runtime.store.setup()
                except BaseException:
                    await runtime.store.aclose()
                    raise
        except WorkerConfigError as exc:
            raise ConfigurationError(str(exc)) from exc
        return runtime

    async def _close(self, runtime: Any) -> None:
        from tenxgraph_api.jobs.config.factory import close_runtime

        await close_runtime(runtime)

    def _emit(self, title: str, data: dict[str, Any]) -> None:
        self.output.print_key_value_pairs(data, title=title)

    # ------------------------------------------------------------------ enqueue

    def _identity(self, cfg: Any, identity: str | None, user: str | None) -> dict[str, Any]:
        if (identity is None) == (user is None):
            raise ValidationError(
                "give exactly one of --identity NAME (a worker.system_identities entry) "
                "or --user USER_ID",
                field="identity",
            )
        if identity is not None:
            resolved = cfg.system_identity(identity)
            if resolved is None:
                known = ", ".join(sorted(cfg.system_identities)) or "none defined"
                raise ValidationError(
                    f"unknown system identity {identity!r} (worker.system_identities: {known})",
                    field="identity",
                )
            return resolved
        from tenxgraph_api.jobs.core.identity import user_identity

        assert user is not None  # noqa: S101 - checked above
        return user_identity(user)

    def _do_enqueue(  # - one argument per flag
        self,
        config: str,
        name: str,
        kwargs: str = "{}",
        key: str | None = None,
        queue: str | None = None,
        priority: str | None = None,
        delay: float | None = None,
        identity: str | None = None,
        user: str | None = None,
    ) -> int:
        try:
            parsed = json.loads(kwargs)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"--kwargs is not valid JSON: {exc}", field="kwargs") from exc
        if not isinstance(parsed, dict):
            raise ValidationError("--kwargs must be a JSON object", field="kwargs")
        if priority is not None and priority not in PRIORITY_CHOICES:
            raise ValidationError(
                f"--priority must be one of {', '.join(PRIORITY_CHOICES)}", field="priority"
            )
        cfg, base_dir = self._load(config)
        resolved = self._identity(cfg, identity, user)

        from tenxgraph_api.jobs.config.config import WorkerConfigError
        from tenxgraph_api.jobs.config.factory import import_task_modules
        from tenxgraph_api.jobs.core import registry
        from tenxgraph_api.jobs.core.runtime import use_runtime
        from tenxgraph_api.jobs.errors import JobsError, TaskNotFound

        try:
            import_task_modules(cfg.modules)
        except WorkerConfigError as exc:
            raise ConfigurationError(str(exc)) from exc
        try:
            task = registry.get_task(name)
        except TaskNotFound as exc:
            known = ", ".join(sorted(registry.all_tasks())) or "none registered"
            raise ValidationError(f"{exc} (registered: {known})", field="name") from exc
        if queue is not None and queue != task.queue:
            raise ValidationError(
                f"task {name!r} is declared for queue {task.queue!r}; --queue {queue!r} "
                "does not match (the queue is part of the task declaration)",
                field="queue",
            )

        async def go() -> Any:
            from tenxgraph_api.jobs.core.client import enqueue

            runtime = await self._open(cfg, base_dir, full=True)
            try:
                with use_runtime(runtime):
                    handle = await enqueue(
                        name,
                        parsed,
                        identity=resolved,
                        key=key,
                        priority=priority,
                        delay=delay,
                        trigger={"type": "cli"},
                    )
                    return await handle.status()
            finally:
                await self._close(runtime)

        try:
            run = asyncio.run(go())
        except JobsError as exc:
            raise ValidationError(str(exc), field="kwargs") from exc
        if self._structured:
            self._emit("enqueued", run.model_dump(mode="json"))
        else:
            self.output.success(f"Enqueued {run.task}: run {run.run_id} ({run.state.value})")
        return 0

    # ------------------------------------------------------------------ runs, run

    def _do_runs(  # - one argument per flag
        self,
        config: str,
        state: str | None = None,
        task: str | None = None,
        queue: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> int:
        from tenxgraph_api.jobs.core.models import RunFilter, RunState

        if state is not None and state not in {s.value for s in RunState}:
            raise ValidationError(
                f"--state must be one of {', '.join(s.value for s in RunState)}", field="state"
            )
        cfg, base_dir = self._load(config)
        self._preflight(cfg, base_dir)

        async def go() -> Any:
            runtime = await self._open(cfg, base_dir, full=False)
            try:
                return await runtime.store.list_runs(
                    RunFilter(task=task, state=state, queue=queue), limit=limit, cursor=cursor
                )
            finally:
                await self._close(runtime)

        try:
            page = asyncio.run(go())
        except ValueError as exc:
            raise ValidationError(f"invalid --cursor: {exc}", field="cursor") from exc
        if self._structured:
            self._emit(
                "runs",
                {
                    "items": [r.model_dump(mode="json") for r in page.items],
                    "next_cursor": page.next_cursor,
                },
            )
            return 0
        if not page.items:
            self.output.info("No runs found.", emoji=False)
            return 0
        self.output.print_table(
            ["run_id", "task", "state", "queue", "priority", "attempt", "enqueued"],
            [
                [
                    r.run_id,
                    r.task,
                    r.state.value,
                    r.queue,
                    r.priority,
                    str(r.attempt),
                    _when(r.enqueued_at),
                ]
                for r in page.items
            ],
        )
        if page.next_cursor:
            self.output.info(f"More runs: --cursor {page.next_cursor}", emoji=False)
        return 0

    def _do_run(self, config: str, run_id: str) -> int:
        from tenxgraph_api.jobs.core.client import RunHandle
        from tenxgraph_api.jobs.errors import RunNotFound

        cfg, base_dir = self._load(config)
        self._preflight(cfg, base_dir)

        async def go() -> tuple[Any, list[Any]]:
            runtime = await self._open(cfg, base_dir, full=False)
            try:
                handle = RunHandle(run_id, runtime)
                run = await handle.status()
                recent: deque[Any] = deque(maxlen=RECENT_EVENTS)
                async for event in handle.events():
                    recent.append(event)
                return run, list(recent)
            finally:
                await self._close(runtime)

        try:
            run, events = asyncio.run(go())
        except RunNotFound as exc:
            raise _jobs_error(str(exc)) from exc
        if self._structured:
            self._emit(
                "run",
                {"run": run.model_dump(mode="json"), "events": [e.model_dump() for e in events]},
            )
            return 0
        pairs: dict[str, Any] = {
            "run_id": run.run_id,
            "task": run.task,
            "state": run.state.value,
            "queue": run.queue,
            "priority": run.priority,
            "attempt": f"{run.attempt} (retries {run.failure_count}/{run.max_retries})",
            "owner": run.owner_user_id or "-",
            "worker": run.worker_id or "-",
            "enqueued": _when(run.enqueued_at),
            "started": _when(run.started_at),
            "finished": _when(run.finished_at),
            "kwargs": json.dumps(run.kwargs, default=str),
        }
        if run.result is not None:
            pairs["result"] = json.dumps(run.result, default=str)
        if run.error:
            pairs["error"] = json.dumps(run.error, default=str)
        self.output.print_key_value_pairs(pairs, title=f"Run {run.run_id}")
        if events:
            self.output.print_table(
                ["seq", "attempt", "type", "at", "data"],
                [
                    [
                        str(e.seq),
                        str(e.attempt),
                        e.type,
                        _when(e.at),
                        json.dumps(e.data, default=str),
                    ]
                    for e in events
                ],
                title=f"Last {len(events)} events",
            )
        return 0

    # ------------------------------------------------------------------ cancel

    def _do_cancel(self, config: str, run_id: str) -> int:
        from tenxgraph_api.jobs.core.client import RunHandle
        from tenxgraph_api.jobs.errors import RunNotFound

        cfg, base_dir = self._load(config)
        self._preflight(cfg, base_dir)

        async def go() -> Any:
            runtime = await self._open(cfg, base_dir, full=False)
            try:
                handle = RunHandle(run_id, runtime)
                current = await handle.status()
                return current if current.terminal else await handle.cancel()
            finally:
                await self._close(runtime)

        try:
            run = asyncio.run(go())
        except RunNotFound as exc:
            raise _jobs_error(str(exc)) from exc
        if self._structured:
            self._emit("cancel", run.model_dump(mode="json"))
        elif run.state.value == "cancelling":
            self.output.success(f"Cancel requested for run {run_id}; the worker will stop it")
        elif run.state.value == "cancelled":
            self.output.success(f"Run {run_id} cancelled")
        else:
            self.output.info(f"Run {run_id} already finished ({run.state.value})", emoji=False)
        return 0

    # ------------------------------------------------------------------ workers, fire

    def _do_workers(self, config: str) -> int:
        self._load(config)
        message = "Worker listing is not available until Sprint 4 (worker self-registration)."
        if self._structured:
            self._emit("workers", {"available": False, "message": message})
        else:
            self.output.info(message, emoji=False)
        return 0

    def _do_fire(self, config: str, name: str) -> int:
        raise _jobs_error(
            f"`tasks fire {name}` needs schedules, which arrive in Sprint 4; nothing was run",
            exit_code=2,
        )

    # ------------------------------------------------------------------ migrate

    def _do_migrate(self, config: str, check: bool = False) -> int:
        cfg, base_dir = self._load(config)
        kind = cfg.store_type
        report: dict[str, Any] = {"store": kind, "check": check}
        outdated = False

        if kind == "postgres":
            from tenxgraph_api.jobs.store.postgres import CURRENT_SCHEMA_VERSION
            from tenxgraph_api.jobs.store.postgres import migrate as pg_migrate

            assert cfg.store is not None  # noqa: S101 - postgres has a store section
            version = asyncio.run(
                pg_migrate(cfg.store.expanded_url(), cfg.store.db_schema, check=check)
            )
            report.update(version=version, target=CURRENT_SCHEMA_VERSION)
            outdated = version < CURRENT_SCHEMA_VERSION
        elif kind == "sqlite":
            from tenxgraph_api.jobs.store.sqlite import STORE_MIGRATIONS
            from tenxgraph_api.jobs.transport.sqlite import TRANSPORT_MIGRATIONS

            store_path = _resolve(cfg.store_path, base_dir)
            transport_path = _resolve(cfg.transport_path, base_dir)
            targets = {"store": len(STORE_MIGRATIONS), "transport": len(TRANSPORT_MIGRATIONS)}
            if not check:

                async def go() -> None:
                    runtime = await self._open(cfg, base_dir, full=True)
                    await self._close(runtime)

                asyncio.run(go())
            found = {
                "store": (self._sqlite_versions(store_path) or {}).get("store", 0),
                "transport": (self._sqlite_versions(transport_path) or {}).get("transport", 0),
            }
            report.update(version=found, target=targets, path=str(store_path))
            outdated = any(found[c] < targets[c] for c in targets)
        elif check:
            report["message"] = f"check not supported for {kind}"
        else:

            async def go_redis() -> None:
                runtime = await self._open(cfg, base_dir, full=True)
                await self._close(runtime)

            asyncio.run(go_redis())
            report["message"] = f"{kind} needs no schema; connection verified"

        report["up_to_date"] = not outdated
        if self._structured:
            self._emit("migrate", report)
        elif "message" in report:
            self.output.info(str(report["message"]), emoji=False)
        elif outdated:
            self.output.warning(
                f"Jobs schema is out of date ({report['version']} < {report['target']}); "
                "run 10xgraph tasks migrate",
                emoji=False,
            )
        else:
            self.output.success(f"Jobs schema is up to date ({report['version']})")
        return 1 if (check and outdated) else 0
