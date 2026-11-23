"""Read a module's names at call time, by module name."""

import sys
from typing import Any


class LateModule:
    """A module looked up in ``sys.modules`` on every attribute read.

    A module that was split in two reads the names tests patch on the original module through
    this, so a patch of the original's name still takes effect in the code that moved out.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str) -> Any:
        return getattr(sys.modules[self._name], attr)
