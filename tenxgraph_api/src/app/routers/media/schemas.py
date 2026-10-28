"""Schemas for the media / file upload endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field


class FileUploadResponse(BaseModel):
    file_id: str = Field(..., description="Opaque storage key")
    mime_type: str
    size_bytes: int
    filename: str
    extracted_text: str | None = None
    url: str = Field(..., description="Relative retrieval URL")
    direct_url: str | None = None
    direct_url_expires_at: int | None = None


class FileInfoResponse(BaseModel):
    file_id: str
    mime_type: str
    size_bytes: int
    filename: str | None = None
    extracted_text: str | None = None
    direct_url: str | None = None
    direct_url_expires_at: int | None = None


class FileDeleteResponse(BaseModel):
    file_id: str
    deleted: bool = True


class FileAccessUrlResponse(BaseModel):
    file_id: str
    url: str
    expires_at: int | None = None
    mime_type: str


class MultimodalConfigResponse(BaseModel):
    media_storage_type: str
    media_max_size_mb: float
    document_handling: str


class FileListItem(BaseModel):
    file_id: str
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    url: str = Field(..., description="Relative retrieval URL")
    created_at: int | None = Field(
        None, description="Upload time, unix seconds; null for files uploaded before it was kept"
    )


class FileListResponse(BaseModel):
    files: list[FileListItem]
    has_more: bool = Field(..., description="Whether another page follows")
    offset: int
    limit: int
