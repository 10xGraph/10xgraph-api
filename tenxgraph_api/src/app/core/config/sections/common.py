"""Parsing helpers shared by the config sections."""

import logging
import os
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    pass


logger = logging.getLogger("tenxgraph_api")


def _parse_bool(value: object, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"{field} must be a boolean")


def _expand_env(value: str | None) -> str | None:
    if value is None:
        return None
    expanded = os.path.expandvars(value)
    if expanded == value and (value.startswith("$") or "${" in value):
        raise ValueError(f"Unresolved environment variable in value: {value}")
    return expanded
