"""Upload filename cleaning, a single file part, and the quiet 501 for stores that cannot list."""

import io
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.datastructures import FormData, UploadFile
from tenxgraph.storage.media.security import sanitize_filename

from tenxgraph_api.src.app.routers.media.router import (
    _uploaded_file,
    list_files,
)


@pytest.mark.parametrize(
    ("sent", "stored"),
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\bob\\report.pdf", "report.pdf"),
        ("a\x00b.txt", "ab.txt"),
        ("line\r\nbreak.txt", "linebreak.txt"),
        ("tab\x85\x9fx.txt", "tabx.txt"),
        ("invoice\u202egpj.exe", "invoicegpj.exe"),
        ("\u2066x\u2069.txt", "x.txt"),
        ("  spaced.txt  ", "spaced.txt"),
        ("", "unnamed"),
        ("..", "unnamed"),
        ("dir/", "unnamed"),
        ("\u202e\x00", "unnamed"),
        (
            "r\u00e9sum\u00e9 \u65e5\u672c\u8a9e 2026.pdf",
            "r\u00e9sum\u00e9 \u65e5\u672c\u8a9e 2026.pdf",
        ),
        (".env", "env"),
    ],
)
def test_clean_filename(sent, stored):
    # Uploads use core's sanitize_filename.
    assert sanitize_filename(sent) == stored


def test_clean_filename_is_capped():
    assert len(sanitize_filename("a" * 1000 + ".txt")) == 255


def _request(*names_and_files):
    request = MagicMock()
    request.form = AsyncMock(return_value=FormData(list(names_and_files)))
    return request


def _part(name):
    return UploadFile(io.BytesIO(b"x"), filename=name)


@pytest.mark.asyncio
async def test_two_file_parts_are_refused():
    request = _request(("file", _part("a.txt")), ("file", _part("b.txt")))
    with pytest.raises(HTTPException) as exc:
        await _uploaded_file(request, None)
    assert exc.value.status_code == 422
    assert exc.value.detail == "Send exactly one file part"


@pytest.mark.asyncio
async def test_one_file_part_is_accepted():
    part = _part("a.txt")
    assert await _uploaded_file(_request(("file", part)), None) is part


@pytest.mark.asyncio
async def test_missing_file_part_is_still_422():
    with pytest.raises(HTTPException) as exc:
        await _uploaded_file(_request(("other", "x")), None)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_store_that_cannot_list_logs_one_warning_without_traceback(caplog):
    service = AsyncMock()
    service.list_files.side_effect = NotImplementedError("no listing")
    with caplog.at_level(logging.DEBUG), pytest.raises(HTTPException) as exc:
        await list_files(MagicMock(), limit=50, offset=0, service=service, identity=None)
    assert exc.value.status_code == 501
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
