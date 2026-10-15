"""Tests for media router."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, UploadFile


@pytest.fixture
def mock_request():
    """Mock FastAPI request."""
    request = MagicMock()
    request.state.request_id = "test-request-id"
    request.state.timestamp = "2024-01-01T00:00:00Z"
    return request


@pytest.fixture
def mock_service():
    """Mock MediaService (not a local filesystem store, so downloads use get_file)."""
    service = AsyncMock()
    service.local_file.return_value = None
    return service


@pytest.fixture
def mock_user():
    """Mock authenticated user."""
    return {"id": "user-123", "user_id": "user-123", "name": "Test User"}


class TestUploadFileLogic:
    """Test POST /v1/files/upload endpoint logic."""

    @pytest.mark.asyncio
    async def test_upload_file_validates_filename(self, mock_request, mock_service, mock_user):
        """Test that upload_file validates filename."""
        from tenxgraph_api.src.app.routers.media.router import upload_file

        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = None
        mock_file.read = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await upload_file(
                request=mock_request,
                file=mock_file,
                service=mock_service,
                user=mock_user,
            )
        assert exc_info.value.status_code == 400
        assert "filename" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_upload_file_validates_empty_file(self, mock_request, mock_service, mock_user):
        """Test that upload_file validates empty file."""
        from tenxgraph_api.src.app.routers.media.router import upload_file

        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = "test.txt"
        mock_file.read = AsyncMock(return_value=b"")

        with pytest.raises(HTTPException) as exc_info:
            await upload_file(
                request=mock_request,
                file=mock_file,
                service=mock_service,
                user=mock_user,
            )
        assert exc_info.value.status_code == 400
        assert "empty" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    @patch("tenxgraph_api.src.app.routers.media.router.success_response")
    async def test_upload_file_calls_service(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test that upload_file calls service."""
        from tenxgraph_api.src.app.routers.media.router import upload_file

        mock_success_response.return_value = {"data": {}}
        mock_service.upload_file.return_value = {
            "file_id": "file-1",
            "filename": "test.txt",
            "mime_type": "text/plain",
            "size_bytes": 100,
            "url": "/v1/files/file-1",
            "direct_url": None,
        }

        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = "test.txt"
        mock_file.content_type = "text/plain"
        # The handler reads in chunks until an empty read signals EOF.
        mock_file.read = AsyncMock(side_effect=[b"test data", b""])

        result = await upload_file(
            request=mock_request,
            file=mock_file,
            service=mock_service,
            user=mock_user,
        )

        mock_service.upload_file.assert_called_once()

    @pytest.mark.asyncio
    async def test_upload_file_handles_service_error(self, mock_request, mock_service, mock_user):
        """Test that upload_file handles service errors."""
        from tenxgraph_api.src.app.routers.media.router import upload_file

        mock_service.upload_file.side_effect = ValueError("File too large")

        mock_file = MagicMock(spec=UploadFile)
        mock_file.filename = "test.txt"
        mock_file.content_type = "text/plain"
        # The handler reads in chunks until an empty read signals EOF.
        mock_file.read = AsyncMock(side_effect=[b"test data", b""])

        with pytest.raises(HTTPException) as exc_info:
            await upload_file(
                request=mock_request,
                file=mock_file,
                service=mock_service,
                user=mock_user,
            )
        assert exc_info.value.status_code == 413


class TestGetFileLogic:
    """Test GET /v1/files/{file_id} endpoint logic."""

    @pytest.mark.asyncio
    async def test_get_file_returns_response(self, mock_service, mock_user):
        """Test that get_file returns file response."""
        from tenxgraph_api.src.app.routers.media.router import get_file

        mock_service.get_file.return_value = (b"file content", "text/plain")

        result = await get_file(
            file_id="file-1",
            service=mock_service,
            user=mock_user,
        )

        mock_service.get_file.assert_called_once_with("file-1", "user-123")
        assert result.body == b"file content"
        assert result.media_type == "text/plain"

    @pytest.mark.asyncio
    async def test_get_file_handles_not_found(self, mock_service, mock_user):
        """Test that get_file handles file not found."""
        from tenxgraph_api.src.app.routers.media.router import get_file

        mock_service.get_file.side_effect = KeyError()

        with pytest.raises(HTTPException) as exc_info:
            await get_file(
                file_id="file-1",
                service=mock_service,
                user=mock_user,
            )
        assert exc_info.value.status_code == 404


class TestGetFileInfoLogic:
    """Test GET /v1/files/{file_id}/info endpoint logic."""

    @pytest.mark.asyncio
    @patch("tenxgraph_api.src.app.routers.media.router.success_response")
    async def test_get_file_info_calls_service(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test that get_file_info calls service."""
        from tenxgraph_api.src.app.routers.media.router import get_file_info

        mock_success_response.return_value = {"data": {}}
        mock_service.get_file_info.return_value = {
            "file_id": "file-1",
            "filename": "test.txt",
            "mime_type": "text/plain",
            "size_bytes": 100,
            "direct_url": None,
        }

        result = await get_file_info(
            request=mock_request,
            file_id="file-1",
            service=mock_service,
            user=mock_user,
        )

        mock_service.get_file_info.assert_called_once_with("file-1", "user-123")

    @pytest.mark.asyncio
    async def test_get_file_info_handles_not_found(self, mock_request, mock_service, mock_user):
        """Test that get_file_info handles file not found."""
        from tenxgraph_api.src.app.routers.media.router import get_file_info

        mock_service.get_file_info.side_effect = KeyError()

        with pytest.raises(HTTPException) as exc_info:
            await get_file_info(
                request=mock_request,
                file_id="file-1",
                service=mock_service,
                user=mock_user,
            )
        assert exc_info.value.status_code == 404


class TestGetFileAccessUrlLogic:
    """Test GET /v1/files/{file_id}/url endpoint logic."""

    @pytest.mark.asyncio
    @patch("tenxgraph_api.src.app.routers.media.router.success_response")
    async def test_get_file_access_url_with_direct_url(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test get_file_access_url with direct URL."""
        from tenxgraph_api.src.app.routers.media.router import get_file_access_url

        mock_success_response.return_value = {"data": {}}
        mock_service.get_file_info.return_value = {
            "file_id": "file-1",
            "filename": "test.txt",
            "mime_type": "text/plain",
            "size_bytes": 100,
            "direct_url": "https://example.com/file-1",
            "direct_url_expires_at": None,
        }

        result = await get_file_access_url(
            request=mock_request,
            file_id="file-1",
            service=mock_service,
            user=mock_user,
        )

        mock_service.get_file_info.assert_called_once_with("file-1")

    @pytest.mark.asyncio
    @patch("tenxgraph_api.src.app.routers.media.router.success_response")
    async def test_get_file_access_url_fallback_url(
        self, mock_success_response, mock_request, mock_service, mock_user
    ):
        """Test get_file_access_url falls back to default URL."""
        from tenxgraph_api.src.app.routers.media.router import get_file_access_url

        mock_success_response.return_value = {"data": {}}
        mock_service.get_file_info.return_value = {
            "file_id": "file-1",
            "filename": "test.txt",
            "mime_type": "text/plain",
            "size_bytes": 100,
            "direct_url": None,
        }

        result = await get_file_access_url(
            request=mock_request,
            file_id="file-1",
            service=mock_service,
            user=mock_user,
        )

        mock_service.get_file_info.assert_called_once_with("file-1")

    @pytest.mark.asyncio
    async def test_get_file_access_url_handles_not_found(
        self, mock_request, mock_service, mock_user
    ):
        """Test get_file_access_url handles file not found."""
        from tenxgraph_api.src.app.routers.media.router import get_file_access_url

        mock_service.get_file_info.side_effect = KeyError()

        with pytest.raises(HTTPException) as exc_info:
            await get_file_access_url(
                request=mock_request,
                file_id="file-1",
                service=mock_service,
                user=mock_user,
            )
        assert exc_info.value.status_code == 404


class TestGetMultimodalConfigLogic:
    """Test GET /v1/config/multimodal endpoint logic."""

    @pytest.mark.asyncio
    @patch("tenxgraph_api.src.app.routers.media.router.get_media_settings")
    @patch("tenxgraph_api.src.app.routers.media.router.success_response")
    async def test_get_multimodal_config(
        self, mock_success_response, mock_get_settings, mock_request, mock_user
    ):
        """Test get_multimodal_config returns config."""
        from tenxgraph_api.src.app.routers.media.router import get_multimodal_config

        mock_settings = MagicMock()
        mock_settings.MEDIA_STORAGE_TYPE.value = "LOCAL"
        mock_settings.MEDIA_STORAGE_PATH = "/tmp/media"
        mock_settings.MEDIA_MAX_SIZE_MB = 100
        mock_settings.DOCUMENT_HANDLING = "extract_text"
        mock_get_settings.return_value = mock_settings

        mock_success_response.return_value = {"data": {}}

        result = await get_multimodal_config(
            request=mock_request,
            user=mock_user,
        )

        mock_get_settings.assert_called_once()
        mock_success_response.assert_called_once()
