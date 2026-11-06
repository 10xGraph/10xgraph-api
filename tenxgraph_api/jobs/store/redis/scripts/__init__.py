"""The run store's Lua scripts, one ``.lua`` file each, every one prefixed by ``prelude.lua``."""

from __future__ import annotations

from importlib.resources import files

from tenxgraph_api.jobs.core.models import CAP_WAIT_BASE, CAP_WAIT_MAX, EVENT_LOG_CAP


NAMES = (
    "create",
    "list",
    "claim",
    "renew",
    "append",
    "transition",
    "cancel",
    "find_expired",
    "mark_lost",
    "due",
    "mark_published",
    "recount",
    "purge",
)

#: Python constants the scripts share, defined in front of the prelude.
_CONSTANTS = (
    f"local CAP_BASE = {int(CAP_WAIT_BASE * 1_000_000)}\n"
    f"local CAP_MAX = {int(CAP_WAIT_MAX * 1_000_000)}\n"
    f"local EVENT_CAP = {EVENT_LOG_CAP}\n"
)


def _read(name: str) -> str:
    return files(__package__).joinpath(f"{name}.lua").read_text(encoding="utf-8")


def sources() -> dict[str, str]:
    """Every script's full source (constants, prelude, body), by name."""
    prelude = _CONSTANTS + _read("prelude")
    return {name: prelude + "\n" + _read(name) for name in NAMES}
