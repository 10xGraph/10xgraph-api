"""The ``audit`` block of 10xgraph.json."""

import re

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


# "module:Class" (a BaseAuditSink subclass, instance or zero-argument factory).
_IMPORT_PATH = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_][\w.]*$")
BUILTIN_SINKS = ("log", "publisher")


class AuditConfig(BaseModel):
    """``audit`` in 10xgraph.json. Example::

        "audit": {"enabled": true, "sinks": ["log"], "include_reads": true}

    ``include_reads: false`` drops allowed access events; denials, auth failures and changes
    are always kept. Unknown keys refuse to boot.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool = False
    sinks: list[str] = Field(default_factory=lambda: ["log"], min_length=1)
    include_reads: StrictBool = True

    @field_validator("sinks")
    @classmethod
    def _check_sinks(cls, sinks: list[str]) -> list[str]:
        for sink in sinks:
            if sink not in BUILTIN_SINKS and not _IMPORT_PATH.match(sink):
                raise ValueError(
                    f'audit.sinks: unknown sink {sink!r}; use "log", "publisher" or "module:Class"'
                )
        return sinks
