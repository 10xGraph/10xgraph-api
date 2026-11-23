"""Media store and document pipeline factories."""

from __future__ import annotations

import inspect
import json
from typing import Any

from tenxgraph.storage.media.config import DocumentHandling
from tenxgraph.storage.media.storage.base import BaseMediaStore

from tenxgraph_api.src.app.core.config.media_settings import MediaSettings, MediaStorageType
from tenxgraph_api.src.app.core.validation.request_config import ClientInputError
from tenxgraph_api.src.app.utils.media.extractor import DocumentExtractor
from tenxgraph_api.src.app.utils.media.pipeline import DocumentPipeline


def _build_config_instance(config_cls: type[Any], values: dict[str, Any]) -> Any:
    """Instantiate third-party config classes using only supported kwargs."""
    params = inspect.signature(config_cls).parameters
    supported = {
        name: value for name, value in values.items() if name in params and value not in (None, "")
    }
    return config_cls(**supported)


def _create_media_store(settings: MediaSettings) -> BaseMediaStore:
    """Factory that instantiates the right MediaStore from settings."""
    stype = settings.MEDIA_STORAGE_TYPE
    # The store enforces the upload cap itself, so offloaded and other non-upload writes
    # cannot go past it either.
    max_bytes = int(settings.MEDIA_MAX_SIZE_MB * 1024 * 1024)

    if stype == MediaStorageType.MEMORY:
        from tenxgraph.storage.media.storage.memory_store import InMemoryMediaStore

        return InMemoryMediaStore(max_size_bytes=max_bytes)

    if stype == MediaStorageType.LOCAL:
        from tenxgraph.storage.media.storage.local_store import LocalFileMediaStore

        return LocalFileMediaStore(base_dir=settings.MEDIA_STORAGE_PATH, max_size_bytes=max_bytes)

    if stype == MediaStorageType.CLOUD:
        from cloud_storage_manager.storage_provider import CloudStorageFactory, StorageProvider
        from cloud_storage_manager.storage_schema import AwsConfig, StorageConfig
        from tenxgraph.storage.media.storage.cloud_store import CloudMediaStore

        provider_name = settings.MEDIA_CLOUD_PROVIDER.lower()
        provider = getattr(StorageProvider, provider_name.upper(), None)
        if provider is None and provider_name == "gcs":
            provider = getattr(StorageProvider, "GCP", None)
        if provider is None:
            raise ValueError(f"Unsupported MEDIA_CLOUD_PROVIDER: {settings.MEDIA_CLOUD_PROVIDER}")

        storage_kwargs: dict[str, Any] = {}

        if provider_name == "aws":
            storage_kwargs["aws"] = _build_config_instance(
                AwsConfig,
                {
                    "bucket_name": settings.MEDIA_CLOUD_BUCKET,
                    "region": settings.MEDIA_CLOUD_REGION,
                    "region_name": settings.MEDIA_CLOUD_REGION,
                    "access_key_id": settings.MEDIA_CLOUD_ACCESS_KEY_ID,
                    "secret_access_key": settings.MEDIA_CLOUD_SECRET_ACCESS_KEY,
                    "session_token": settings.MEDIA_CLOUD_SESSION_TOKEN,
                },
            )
        else:
            from cloud_storage_manager.storage_schema import GcpConfig

            credentials = None
            if settings.MEDIA_CLOUD_CREDENTIALS_JSON:
                try:
                    credentials = json.loads(settings.MEDIA_CLOUD_CREDENTIALS_JSON)
                except json.JSONDecodeError as exc:
                    raise ValueError("MEDIA_CLOUD_CREDENTIALS_JSON must be valid JSON") from exc

            storage_kwargs["gcp"] = _build_config_instance(
                GcpConfig,
                {
                    "bucket_name": settings.MEDIA_CLOUD_BUCKET,
                    "project_id": settings.MEDIA_CLOUD_PROJECT_ID,
                    "credentials": credentials,
                    "credentials_json": credentials,
                },
            )

        storage = CloudStorageFactory.get_storage(provider, StorageConfig(**storage_kwargs))
        return CloudMediaStore(
            storage=storage,
            prefix=settings.MEDIA_CLOUD_PREFIX,
            max_size_bytes=max_bytes,
        )

    raise ValueError(f"Unknown MEDIA_STORAGE_TYPE: {stype}")


def _create_document_pipeline(settings: MediaSettings) -> DocumentPipeline:
    handling_map = {
        "extract_text": DocumentHandling.EXTRACT_TEXT,
        "pass_raw": DocumentHandling.FORWARD_RAW,
        "skip": DocumentHandling.SKIP,
    }
    handling = handling_map.get(settings.DOCUMENT_HANDLING, DocumentHandling.EXTRACT_TEXT)

    extractor = None
    if handling == DocumentHandling.EXTRACT_TEXT:
        try:
            extractor = DocumentExtractor(
                max_pdf_bytes=int(settings.MEDIA_MAX_PDF_MB * 1024 * 1024),
                max_pdf_pages=settings.MEDIA_MAX_PDF_PAGES,
            )
        except ImportError as exc:
            # A server setup problem, reported as a clear 422 instead of a bare 500.
            raise ClientInputError(
                "File uploads are not configured on this server: document text extraction "
                'needs the media extra (pip install "10xgraph-api[media]"), or set '
                "DOCUMENT_HANDLING to pass_raw or skip."
            ) from exc
    return DocumentPipeline(document_extractor=extractor, handling=handling)
