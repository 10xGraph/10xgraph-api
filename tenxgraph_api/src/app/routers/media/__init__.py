"""Media service: provides the configured MediaStore and document extractor."""

from __future__ import annotations

from tenxgraph_api.src.app.routers.media.caching import (
    MAX_EXTRACTION_CHARS,
    MAX_EXTRACTION_ENTRIES,
    MAX_SIGNED_URL_ENTRIES,
    _BoundedCache,
)
from tenxgraph_api.src.app.routers.media.factory import (
    _build_config_instance,
    _create_document_extractor,
    _create_media_store,
)
from tenxgraph_api.src.app.routers.media.ownership import (
    TENANT_KEY,
    THREADS_KEY,
    MissingTenantError,
    _OwnedStore,
    tenant_matches,
)
from tenxgraph_api.src.app.routers.media.service import MediaService


__all__ = [
    "MAX_EXTRACTION_CHARS",
    "MAX_EXTRACTION_ENTRIES",
    "MAX_SIGNED_URL_ENTRIES",
    "TENANT_KEY",
    "THREADS_KEY",
    "MediaService",
    "MissingTenantError",
    "_BoundedCache",
    "_OwnedStore",
    "_build_config_instance",
    "_create_document_extractor",
    "_create_media_store",
    "tenant_matches",
]
