"""Resolve a ``"module:attr"`` config value to the object it names."""

import importlib
from typing import Any


def import_target(path: str) -> Any:
    """Import ``module`` and return ``attr`` from it (a dotted ``attr`` walks the attributes)."""
    module_name, _, attr = path.partition(":")
    target: Any = importlib.import_module(module_name)
    for part in attr.split("."):
        target = getattr(target, part)
    return target
