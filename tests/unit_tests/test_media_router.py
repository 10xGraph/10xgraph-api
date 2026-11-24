"""Tests for media router."""

from unittest.mock import AsyncMock, MagicMock

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
    from tenxgraph.core.authz import Identity

    return Identity(user_id="user-123")


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
                identity=mock_user,
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
                identity=mock_user,
            )
        assert exc_info.value.status_code == 400
        assert "empty" in exc_info.value.detail.lower()


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
                identity=mock_user,
            )
        assert exc_info.value.status_code == 413


class TestGetFileLogic:
    """Test GET /v1/files/{file_id} endpoint logic."""


    @pytest.mark.asyncio
    async def test_get_file_handles_not_found(self, mock_service, mock_user):
        """Test that get_file handles file not found."""
        from tenxgraph_api.src.app.routers.media.router import get_file

        mock_service.get_file.side_effect = KeyError()

        with pytest.raises(HTTPException) as exc_info:
            await get_file(
                file_id="file-1",
                service=mock_service,
                identity=mock_user,
            )
        assert exc_info.value.status_code == 404


class TestGetFileInfoLogic:
    """Test GET /v1/files/{file_id}/info endpoint logic."""


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
                identity=mock_user,
            )
        assert exc_info.value.status_code == 404


class TestGetFileAccessUrlLogic:
    """Test GET /v1/files/{file_id}/url endpoint logic."""



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
                identity=mock_user,
            )
        assert exc_info.value.status_code == 404


