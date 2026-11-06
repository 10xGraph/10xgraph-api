"""The ``worker`` section of ``10xgraph.json`` (``WorkerConfig``).

Example::

    "worker": {
      "modules": ["jobs.interviews", "jobs.cleanup"],
      "store": {"type": "postgres", "url": "${DATABASE_URL}"},
      "transport": {"type": "redis", "url": "${REDIS_URL}"},
      "result_ttl": 604800,
      "system_identities": {"nightly": {"user_id": "__system__:nightly"}}
    }

Local development: ``{"modules": "jobs", "backend": "local"}`` (SQLite store and transport in
one file, ``.10xgraph/jobs.db`` next to ``10xgraph.json``).

Parsing never touches the network or the disk. ``url`` keeps ``${VAR}`` as written;
``expanded_url()`` expands it when the backend is built, and an unset variable is a config
error there. Only ``${VAR}`` is expanded: a bare ``$`` (a password such as ``pa$word``) is
kept as written. Unknown keys are config errors that list the
valid keys. Without a ``worker`` section nothing in this package is built or created.
"""

from __future__ import annotations

import os
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from tenxgraph_api.jobs.store.sqlite import DEFAULT_PATH as DEFAULT_SQLITE_PATH


DEFAULT_REDIS_DB = 1
DEFAULT_RESULT_TTL = 604800.0
DEFAULT_LEASE_TTL = 60.0
DEFAULT_SHUTDOWN_TIMEOUT = 30.0

STORE_TYPES = ("postgres", "redis", "sqlite", "memory")
TRANSPORT_TYPES = ("redis", "sqlite", "memory")

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


class WorkerConfigError(ValueError):
    """The ``worker`` section is invalid. The message names the key and what to write."""


def expand_env(value: str, *, field: str) -> str:
    """``value`` with each ``${VAR}`` expanded (a bare ``$`` is kept); an unset variable
    raises ``WorkerConfigError`` naming ``field`` and the variable (never the value, which may
    hold a secret)."""
    missing = sorted({name for name in _ENV_RE.findall(value) if name not in os.environ})
    if missing:
        names = ", ".join("${" + name + "}" for name in missing)
        raise WorkerConfigError(f"{field} uses environment variables that are not set: {names}")
    return _ENV_RE.sub(lambda m: os.environ[m.group(1)], value)


def _check_keys(data: Any, model: type[BaseModel], where: str) -> Any:
    if not isinstance(data, dict):
        return data
    valid = [(f.alias or name) for name, f in model.model_fields.items()]
    unknown = sorted(set(data) - set(valid))
    if unknown:
        raise WorkerConfigError(f"{where}: unknown keys {unknown}; valid keys: {', '.join(valid)}")
    return data


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


class StoreConfig(_Strict):
    """``worker.store``: where runs live (the source of truth)."""

    type: Literal["postgres", "redis", "sqlite", "memory"]
    #: postgres / redis: required; ``${VAR}`` expanded when built.
    url: str | None = None
    #: sqlite: the file, relative to the directory of 10xgraph.json.
    path: str | None = None
    #: postgres: schema of the ``tenxgraph_task_*`` tables.
    db_schema: str = Field(default="public", alias="schema")
    #: postgres: create or upgrade the tables in ``setup()``; false refuses old tables.
    auto_migrate: bool = True

    @model_validator(mode="before")
    @classmethod
    def _keys(cls, data: Any) -> Any:
        return _check_keys(data, cls, "worker.store")

    @model_validator(mode="after")
    def _shape(self) -> StoreConfig:
        _check_url_and_path(self.type, self.url, self.path, "worker.store", ("postgres", "redis"))
        if self.type != "postgres" and self.db_schema != "public":
            raise ValueError('worker.store.schema goes with "type": "postgres"')
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", self.db_schema):
            raise ValueError("worker.store.schema must be a plain SQL identifier")
        return self

    def expanded_url(self) -> str:
        return expand_env(self.url or "", field="worker.store.url")


class TransportConfig(_Strict):
    """``worker.transport``: what carries "run X is ready" to a worker."""

    type: Literal["redis", "sqlite", "memory"]
    url: str | None = None
    path: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _keys(cls, data: Any) -> Any:
        return _check_keys(data, cls, "worker.transport")

    @model_validator(mode="after")
    def _shape(self) -> TransportConfig:
        _check_url_and_path(self.type, self.url, self.path, "worker.transport", ("redis",))
        return self

    def expanded_url(self) -> str:
        return expand_env(self.url or "", field="worker.transport.url")


def _check_url_and_path(
    kind: str, url: str | None, path: str | None, where: str, url_types: tuple[str, ...]
) -> None:
    if kind in url_types:
        if not isinstance(url, str) or not url.strip():
            example = "${DATABASE_URL}" if kind == "postgres" else "${REDIS_URL}"
            raise ValueError(
                f'{where}.url is required with "type": "{kind}" (for example "{example}"); '
                "a missing URL is never replaced by a default"
            )
    elif url is not None:
        raise ValueError(f"{where}.url goes with " + " or ".join(f'"{t}"' for t in url_types))
    if path is not None and kind != "sqlite":
        raise ValueError(f'{where}.path goes with "type": "sqlite"')
    if path is not None and (not isinstance(path, str) or not path.strip()):
        raise ValueError(f"{where}.path must be a file path")


class SystemIdentityConfig(BaseModel):
    """One ``worker.system_identities`` entry: ``user_id`` plus any extra claims."""

    model_config = ConfigDict(extra="allow", frozen=True)

    user_id: str

    @field_validator("user_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("user_id must not be empty")
        return value


class WorkerConfig(_Strict):
    """``worker`` in 10xgraph.json. See the module docstring."""

    modules: list[str] = Field(default_factory=list)
    backend: Literal["local"] | None = None
    store: StoreConfig | None = None
    transport: TransportConfig | None = None
    redis_db: int = Field(default=DEFAULT_REDIS_DB, ge=0)
    result_ttl: float | None = Field(default=DEFAULT_RESULT_TTL, gt=0)
    system_identities: dict[str, SystemIdentityConfig] = Field(default_factory=dict)
    hooks: list[str] = Field(default_factory=list)
    publish_events: bool = True
    lease_ttl: float = Field(default=DEFAULT_LEASE_TTL, gt=0)
    shutdown_timeout: float = Field(default=DEFAULT_SHUTDOWN_TIMEOUT, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _keys(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            raise WorkerConfigError(
                'worker must be an object, for example {"modules": "jobs", "backend": "local"}'
            )
        data = _check_keys(data, cls, "worker")
        data = dict(data)
        for key in ("modules", "hooks"):
            if isinstance(data.get(key), str):
                data[key] = [data[key]]
        return data

    @field_validator("modules")
    @classmethod
    def _modules(cls, value: list[str]) -> list[str]:
        for name in value:
            if not isinstance(name, str) or not _MODULE_RE.match(name):
                raise ValueError(f"{name!r} is not a dotted module path (for example jobs.emails)")
        return list(dict.fromkeys(value))

    @field_validator("hooks")
    @classmethod
    def _hooks(cls, value: list[str]) -> list[str]:
        for path in value:
            module, _, attr = path.partition(":")
            if not attr or not _MODULE_RE.match(module):
                raise ValueError(f"{path!r} must be 'module:attr' naming a TaskLifecycleHook")
        return value

    @model_validator(mode="after")
    def _backends(self) -> WorkerConfig:
        if self.backend == "local":
            if self.store is not None or self.transport is not None:
                raise ValueError(
                    'worker.backend "local" sets both store and transport (SQLite file '
                    f"{DEFAULT_SQLITE_PATH}); remove worker.store and worker.transport, or "
                    "remove backend"
                )
            return self
        if self.store is None or self.transport is None:
            raise ValueError(
                'worker needs "store" and "transport", or "backend": "local" for local '
                'development (for example {"store": {"type": "postgres", "url": '
                '"${DATABASE_URL}"}, "transport": {"type": "redis", "url": "${REDIS_URL}"}})'
            )
        if self.transport.type == "sqlite":
            if self.store.type != "sqlite":
                raise ValueError('worker.transport "sqlite" needs worker.store "sqlite"')
            if self.store_path != self.transport_path:
                raise ValueError("the sqlite store and transport must use the same file")
        if (self.store.type == "memory") != (self.transport.type == "memory"):
            raise ValueError(
                'worker.store and worker.transport "memory" go together (one process only)'
            )
        return self

    # ------------------------------------------------------------------ resolved values

    @property
    def store_type(self) -> str:
        return "sqlite" if self.backend == "local" else self.store.type  # type: ignore[union-attr]

    @property
    def transport_type(self) -> str:
        if self.backend == "local":
            return "sqlite"
        return self.transport.type  # type: ignore[union-attr]

    @property
    def store_path(self) -> str:
        """The SQLite file of the store (``.10xgraph/jobs.db`` unless ``path`` is set)."""
        return (self.store.path if self.store is not None else None) or DEFAULT_SQLITE_PATH

    @property
    def transport_path(self) -> str:
        return (self.transport.path if self.transport is not None else None) or self.store_path

    def system_identity(self, name: str) -> dict[str, Any] | None:
        """The ``system_identities`` entry ``name`` as an identity dict, or None."""
        entry = self.system_identities.get(name)
        return None if entry is None else entry.model_dump(mode="json")


def _format_error(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in ("worker", *err["loc"]))
        message = str(err["msg"]).removeprefix("Value error, ")
        if err["type"] == "literal_error" and loc.endswith(".type"):
            message = f"must be one of {err['ctx']['expected']}"  # type: ignore
        lines.append(f"{loc}: {message}" if not message.startswith("worker") else message)
    return "; ".join(lines)


def parse_worker_config(value: object) -> WorkerConfig | None:
    """The parsed ``worker`` section, or None when it is absent. Raises ``WorkerConfigError``."""
    if value is None:
        return None
    try:
        return WorkerConfig.model_validate(value)
    except WorkerConfigError:
        raise
    except ValidationError as exc:
        raise WorkerConfigError(_format_error(exc)) from None


__all__ = [
    "DEFAULT_REDIS_DB",
    "STORE_TYPES",
    "TRANSPORT_TYPES",
    "StoreConfig",
    "SystemIdentityConfig",
    "TransportConfig",
    "WorkerConfig",
    "WorkerConfigError",
    "expand_env",
    "parse_worker_config",
]
