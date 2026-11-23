"""The ``module:attr`` config value: one format rule and one resolver for the CLI and server."""

import importlib
import re
from typing import Any


# The module part allows ``-``: importlib loads a folder such as ``my-pkg`` and the server
# always accepted it. The attribute part is dotted identifiers.
IMPORT_PATH = re.compile(r"^[\w-]+(?:\.[\w-]+)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")


def is_import_path(value: object) -> bool:
    """True for a string like ``"graph.react:app"`` (a dotted ``attr`` is allowed)."""
    return isinstance(value, str) and IMPORT_PATH.match(value) is not None


def import_target(path: str) -> Any:
    """Import ``module`` and return ``attr`` from it (a dotted ``attr`` walks the attributes)."""
    module_name, _, attr = path.partition(":")
    target: Any = importlib.import_module(module_name)
    for part in attr.split("."):
        target = getattr(target, part)
    return target
