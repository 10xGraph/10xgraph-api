"""File upload, retrieval, and multimodal config endpoints."""

from __future__ import annotations

import mimetypes
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile
from fastapi.logger import logger
from fastapi.responses import FileResponse, Response
from injectq.integrations import InjectAPI

from tenxgraph_api.src.app.core.auth.permissions import RequirePermission
from tenxgraph_api.src.app.core.auth.request_config import client_error_detail
from tenxgraph_api.src.app.core.config.media_settings import get_media_settings
from tenxgraph_api.src.app.core.utils.log_sanitizer import log_safe
from tenxgraph_api.src.app.routers.media import MediaService
from tenxgraph_api.src.app.routers.media.schemas import (
    FileAccessUrlResponse,
    FileDeleteResponse,
    FileInfoResponse,
    FileListItem,
    FileListResponse,
    FileUploadResponse,
    MultimodalConfigResponse,
)
from tenxgraph_api.src.app.utils import success_response
from tenxgraph_api.src.app.utils.media.signatures import content_matches_mime


router = APIRouter(tags=["Files"])

# Uploads without a known size are read in 1 MiB chunks, so a size-cap breach is caught
# before the whole body is buffered in memory.
_UPLOAD_CHUNK_BYTES = 1024 * 1024

# Stored types come from the uploader. Only types a browser displays without running
# script are served inline; the rest are downloads.
_INLINE_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/bmp",
        "application/pdf",
        "text/plain",
    }
)
MAX_FILE_LIST_LIMIT = 200
_INLINE_PREFIXES = ("audio/", "video/")


def _is_active_type(mime: str) -> bool:
    """Whether a browser would run this type as a page or script, even from a blob: URL.

    Such files are served as opaque bytes, so neither a navigation nor ``response.blob()``
    keeps the active type.
    """
    subtype = mime.partition("/")[2]
    return (
        subtype in {"html", "xml", "xhtml+xml", "svg+xml"}
        or subtype.endswith("+xml")
        or "javascript" in subtype
        or "ecmascript" in subtype
    )


async def _read_upload(file: UploadFile, max_bytes: int, max_mb: float) -> bytes:
    """Read an upload into memory once, refusing it past ``max_bytes``.

    Starlette has already spooled the part (to disk past 1 MiB) and knows its size, so a
    known size is checked up front and the file read in one piece: one copy in memory, not
    a chunk list plus the joined result. Without a size, read in chunks with a running cap
    so an oversized body is refused before it is all in memory.
    """
    too_large = HTTPException(status_code=413, detail=f"File exceeds maximum {max_mb}MB")
    size = getattr(file, "size", None)
    if isinstance(size, int):
        if size > max_bytes:
            raise too_large
        data = await file.read()
        if len(data) > max_bytes:  # the part was larger than declared
            raise too_large
        return data

    buffer = bytearray()
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_BYTES)
        if not chunk:
            return bytes(buffer)
        buffer += chunk
        if len(buffer) > max_bytes:
            raise too_large


def _download_headers(file_id: str, mime_type: str | None) -> tuple[str, dict[str, str]]:
    """The served media type and headers, so an uploaded HTML or SVG file cannot run script."""
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    if mime in _INLINE_TYPES or mime.startswith(_INLINE_PREFIXES):
        disposition = "inline"
    else:
        disposition = f'attachment; filename="{file_id}"'
        if not mime or _is_active_type(mime):
            mime = "application/octet-stream"
    headers = {"Content-Disposition": disposition, "X-Content-Type-Options": "nosniff"}
    if mime != "application/pdf":  # browsers will not render a PDF under a sandbox CSP
        headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return mime or "application/octet-stream", headers


def _download_response(file_id: str, data: bytes, mime_type: str | None) -> Response:
    """Serve stored bytes so an uploaded HTML or SVG file cannot run script on this origin."""
    mime, headers = _download_headers(file_id, mime_type)
    return Response(content=data, media_type=mime, headers=headers)


def _file_download_response(file_id: str, path: Any, mime_type: str | None) -> Response:
    """Stream a stored file from disk in chunks, with the same headers as the bytes path.

    ``FileResponse`` reads in small chunks on a worker thread, so a large file neither sits in
    memory whole nor blocks the event loop while it is read.
    """
    mime, headers = _download_headers(file_id, mime_type)
    # No filename is passed, so FileResponse adds no Content-Disposition of its own.
    return FileResponse(path, media_type=mime, headers=headers)


# ------------------------------------------------------------------
# 4.1  POST /v1/files/upload
# ------------------------------------------------------------------
@router.post(
    "/v1/files/upload",
    summary="Upload a file (image, audio, document)",
    response_model=FileUploadResponse,
    description=(
        "Accepts a multipart file upload.  Stores binary in the configured "
        "MediaStore and optionally extracts text for documents."
    ),
)
async def upload_file(
    request: Request,
    file: UploadFile,
    service: MediaService = InjectAPI(MediaService),
    user: dict[str, Any] = Depends(RequirePermission("files", "upload")),
):
    if file.filename is None:
        raise HTTPException(status_code=400, detail="filename is required")

    media_settings = get_media_settings()
    mime = file.content_type or mimetypes.guess_type(file.filename)[0] or "application/octet-stream"

    if not media_settings.is_content_type_allowed(mime):
        raise HTTPException(status_code=415, detail=f"Content type not allowed: {mime}")

    # Read with a running size cap instead of ``await file.read()``. A chunked upload
    # (no Content-Length) slips past RequestSizeLimitMiddleware, so a naive full read
    # would let a multi-GB body OOM-kill the worker. Stop as soon as we exceed the
    # limit and reject, never materializing more than max_size + one chunk.
    max_bytes = int(media_settings.MEDIA_MAX_SIZE_MB * 1024 * 1024)
    data = await _read_upload(file, max_bytes, media_settings.MEDIA_MAX_SIZE_MB)
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")

    if not content_matches_mime(data, mime):
        logger.warning(
            "Rejected upload %s: content does not match %s", log_safe(file.filename), log_safe(mime)
        )
        raise HTTPException(
            status_code=415, detail=f"file content does not match {mime.split(';', 1)[0].strip()}"
        )

    logger.info(
        "File upload: %s (%s, %d bytes)", log_safe(file.filename), log_safe(mime), len(data)
    )

    try:
        # Record the uploader as the owner, so the file cannot later be read by a
        # different user who knows the id.
        result = await service.upload_file(data, file.filename, mime, user_id=user.get("user_id"))
    except ValueError as exc:
        raise HTTPException(status_code=413, detail=client_error_detail(exc))

    return success_response(FileUploadResponse(**result), request)


# ------------------------------------------------------------------
# GET /v1/files  — the caller's own files, paged (declared before /{file_id})
# ------------------------------------------------------------------
@router.get(
    "/v1/files",
    summary="List the caller's files",
    response_model=FileListResponse,
    description=(
        "Lists the files the authenticated user uploaded (files without a recorded owner are "
        "not listed), newest first. With authentication off there is no user, so, like the "
        "download route, every file is listed. Returns 501 when the configured media store cannot "
        "enumerate its files."
    ),
)
async def list_files(
    request: Request,
    limit: int = Query(50, ge=1, le=MAX_FILE_LIST_LIMIT),
    offset: int = Query(0, ge=0),
    service: MediaService = InjectAPI(MediaService),
    user: dict[str, Any] = Depends(RequirePermission("files", "read")),
):
    user_id = user.get("user_id")
    try:
        items, has_more = await service.list_files(user_id, offset, limit)
    except NotImplementedError:
        raise HTTPException(status_code=501, detail="The configured media store cannot list files")
    payload = FileListResponse(
        files=[FileListItem(**item) for item in items],
        has_more=has_more,
        offset=offset,
        limit=limit,
    )
    return success_response(payload, request)


# ------------------------------------------------------------------
# 4.2  GET /v1/files/{file_id}  — raw binary download
# ------------------------------------------------------------------
@router.get(
    "/v1/files/{file_id}",
    summary="Retrieve file binary",
    description="Returns the raw binary with the correct Content-Type.",
)
async def get_file(
    file_id: str,
    service: MediaService = InjectAPI(MediaService),
    user: dict[str, Any] = Depends(RequirePermission("files", "read")),
):
    try:
        # Authentication alone is not enough: without this, ANY authenticated user
        # who knew a file_id could download another user's file.
        await service.ensure_can_access(file_id, user.get("user_id"))
        local = await service.local_file(file_id)
        if local is not None:  # stream from disk instead of loading the whole file
            path, mime_type = local
            return _file_download_response(file_id, path, mime_type)
        data, mime_type = await service.get_file(file_id, user.get("user_id"))
    except KeyError:
        raise HTTPException(status_code=404, detail="File not found")
    except PermissionError:
        # 404, not 403: do not confirm that someone else's file_id exists.
        raise HTTPException(status_code=404, detail="File not found")

    return _download_response(file_id, data, mime_type)


# ------------------------------------------------------------------
# 4.2  GET /v1/files/{file_id}/info  — metadata only
# ------------------------------------------------------------------
@router.get(
    "/v1/files/{file_id}/info",
    summary="Retrieve file metadata",
    response_model=FileInfoResponse,
)
async def get_file_info(
    request: Request,
    file_id: str,
    service: MediaService = InjectAPI(MediaService),
    user: dict[str, Any] = Depends(RequirePermission("files", "read")),
):
    try:
        await service.ensure_can_access(file_id, user.get("user_id"))
        info = await service.get_file_info(file_id, user.get("user_id"))
    except KeyError:
        raise HTTPException(status_code=404, detail="File not found")
    except PermissionError:
        raise HTTPException(status_code=404, detail="File not found")

    return success_response(FileInfoResponse(**info), request)


@router.get(
    "/v1/files/{file_id}/url",
    summary="Retrieve direct access URL",
    response_model=FileAccessUrlResponse,
)
async def get_file_access_url(
    request: Request,
    file_id: str,
    service: MediaService = InjectAPI(MediaService),
    user: dict[str, Any] = Depends(RequirePermission("files", "read")),
):
    try:
        # Especially important here: this hands out a *signed direct URL*, which
        # bypasses the API entirely. Handing one to a non-owner would leak the file
        # even after this check was added elsewhere.
        await service.ensure_can_access(file_id, user.get("user_id"))
        info = await service.get_file_info(file_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="File not found")
    except PermissionError:
        raise HTTPException(status_code=404, detail="File not found")

    payload = FileAccessUrlResponse(
        file_id=file_id,
        url=info["direct_url"] or f"/v1/files/{file_id}",
        expires_at=info.get("direct_url_expires_at"),
        mime_type=info["mime_type"],
    )
    return success_response(payload, request)


# ------------------------------------------------------------------
# DELETE /v1/files/{file_id}  — owner-only manual cleanup
# ------------------------------------------------------------------
@router.delete(
    "/v1/files/{file_id}",
    summary="Delete a file",
    description=(
        "Deletes the stored file and its metadata. Only the uploader may delete it; a file "
        "that is missing or belongs to someone else gives 404."
    ),
    response_model=FileDeleteResponse,
)
async def delete_file(
    request: Request,
    file_id: str,
    service: MediaService = InjectAPI(MediaService),
    user: dict[str, Any] = Depends(RequirePermission("files", "delete")),
):
    try:
        await service.ensure_can_access(file_id, user.get("user_id"))
        deleted = await service.delete_file(file_id)
    except (KeyError, PermissionError, ValueError):
        # 404, not 403: do not confirm that someone else's file_id exists.
        raise HTTPException(status_code=404, detail="File not found")
    if not deleted:
        raise HTTPException(status_code=404, detail="File not found")

    return success_response(FileDeleteResponse(file_id=file_id, deleted=True), request)


# ------------------------------------------------------------------
# 4.4  GET /v1/config/multimodal  — read current multimodal config
# ------------------------------------------------------------------
@router.get(
    "/v1/config/multimodal",
    summary="Get multimodal configuration",
    response_model=MultimodalConfigResponse,
)
async def get_multimodal_config(
    request: Request,
    user: dict[str, Any] = Depends(RequirePermission("config", "read")),
):
    settings = get_media_settings()
    return success_response(
        MultimodalConfigResponse(
            media_storage_type=settings.MEDIA_STORAGE_TYPE.value,
            media_max_size_mb=settings.MEDIA_MAX_SIZE_MB,
            document_handling=settings.DOCUMENT_HANDLING,
        ),
        request,
    )
