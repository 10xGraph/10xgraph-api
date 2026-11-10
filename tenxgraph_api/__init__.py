"""10xGraph API - A Python API framework for 10xGraph graphs."""

from tenxgraph_api.cli.constants import CLI_VERSION as __version__


__author__ = "Shudipto Trafder"
__email__ = "shudiptotrafder@gmail.com"


# Lets expose few things the user suppose to use
from tenxgraph.core.authz import Identity

from .src.app.core.audit import AuditEvent, BaseAuditSink
from .src.app.core.auth.base_auth import BaseAuth
from .src.app.core.auth.permissions import RequireIdentity, RequirePermission
from .src.app.core.cache_store import (
    BaseCacheStore,
    InMemoryCacheStore,
    RedisCacheStore,
    SlotResult,
)
from .src.app.utils.snowflake_id_generator import SnowFlakeIdGenerator
from .src.app.utils.thread_name_generator import (
    ThreadNameGenerator,
)


__all__ = [
    "AuditEvent",
    "BaseAuditSink",
    "BaseAuth",
    "BaseCacheStore",
    "Identity",
    "InMemoryCacheStore",
    "RedisCacheStore",
    "RequireIdentity",
    "RequirePermission",
    "SlotResult",
    "SnowFlakeIdGenerator",
    "ThreadNameGenerator",
    "__version__",
]
