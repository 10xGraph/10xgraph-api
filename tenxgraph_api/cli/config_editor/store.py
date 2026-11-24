"""Read and atomically write the ``10xgraph.json`` file being edited."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigConflictError(Exception):
    """The file changed on disk after the editor loaded it."""


@dataclass(frozen=True, slots=True)
class LoadedConfig:
    config: dict[str, Any] | None
    version: str | None  # sha256 of the file bytes, None when the file does not exist
    parse_error: str | None = None


class ConfigFileStore:
    """Owns one config file: loads it, detects concurrent edits, and saves it safely.

    ``seed_path`` is read only while ``path`` does not exist: the editor starts from its
    contents, and the first save creates ``path``. This is how a project that still has the
    legacy ``agentflow.json`` moves to ``10xgraph.json`` without the old file being touched.
    """

    def __init__(self, path: Path, seed_path: Path | None = None) -> None:
        self.path = path
        self.seed_path = seed_path

    @property
    def backup_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.bak")

    @property
    def seeded(self) -> bool:
        """True while the editor starts from ``seed_path`` because ``path`` is missing."""
        return (
            not self.path.exists()
            and self.seed_path is not None
            and _read(self.seed_path).config is not None
        )

    def load(self) -> LoadedConfig:
        loaded = _read(self.path)
        if loaded.version is None and self.seed_path is not None:
            seed = _read(self.seed_path)
            if seed.config is not None:
                # Version stays None: the target does not exist yet, so a save creates it.
                return LoadedConfig(config=seed.config, version=None)
        return loaded

    def save(self, config: dict[str, Any], expected_version: str | None) -> str:
        """Write ``config`` and return the new version.

        Raises:
            ConfigConflictError: The file on disk no longer matches ``expected_version``.
        """
        current = _read(self.path)
        if current.version != expected_version:
            raise ConfigConflictError(
                f"{self.path.name} changed on disk after it was loaded. Reload to see the "
                "latest version before saving."
            )

        # A new file keeps the key order of the file it was seeded from.
        ordered = _preserve_key_order(config, current.config or self.load().config or {})
        payload = (json.dumps(ordered, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            shutil.copy2(self.path, self.backup_path)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary_path.replace(self.path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
        return _digest(payload)


def _read(path: Path) -> LoadedConfig:
    if not path.exists():
        return LoadedConfig(config=None, version=None)
    raw = path.read_bytes()
    version = _digest(raw)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return LoadedConfig(config=None, version=version, parse_error=str(exc))
    if not isinstance(data, dict):
        return LoadedConfig(
            config=None, version=version, parse_error="The file is not a JSON object."
        )
    return LoadedConfig(config=data, version=version)


def _preserve_key_order(new: dict[str, Any], old: dict[str, Any]) -> dict[str, Any]:
    """Keep the file's existing top-level key order; append new keys at the end."""
    ordered = {key: new[key] for key in old if key in new}
    ordered.update({key: value for key, value in new.items() if key not in ordered})
    return ordered


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
