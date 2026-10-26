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
