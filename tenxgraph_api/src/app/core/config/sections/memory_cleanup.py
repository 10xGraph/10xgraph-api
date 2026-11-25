"""``memory_cleanup`` section of 10xgraph.json: delete expired memories on a schedule."""

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt


class MemoryCleanupConfig(BaseModel):
    """``memory_cleanup`` in 10xgraph.json. Example::

        "memory_cleanup": {"enabled": true, "interval_seconds": 3600, "batch_size": 1000}

    Off by default: expired memories are hidden from every read, but nothing is deleted unless
    this is on. When on, ``10xgraph scheduler`` runs a task every ``interval_seconds`` that
    calls the store's ``adelete_expired(batch_size)``. It needs a ``worker`` key, a ``store``
    that supports expiry, and one running scheduler. Unknown keys refuse to boot.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: StrictBool = False
    interval_seconds: StrictInt = Field(default=3600, ge=1)
    batch_size: StrictInt = Field(default=1000, ge=1)
