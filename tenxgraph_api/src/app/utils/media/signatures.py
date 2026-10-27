"""Leading-byte checks for uploaded files.

The type of an upload comes from the client's multipart header. For formats with a stable
signature, :func:`content_matches_mime` checks that the first bytes agree with that type, so a
file labelled ``image/png`` really starts like a PNG. Types with no known signature (plain
text, CSV, JSON and so on) are accepted: there is nothing reliable to compare.
"""

from __future__ import annotations

from collections.abc import Callable


_SYNC_MASK = 0xE0


def _prefix(*signatures: bytes) -> Callable[[bytes], bool]:
    return lambda data: data.startswith(signatures)


def _riff(kind: bytes) -> Callable[[bytes], bool]:
    return lambda data: data[:4] == b"RIFF" and data[8:12] == kind


def _mp3(data: bytes) -> bool:
    # An ID3 tag, or an MPEG audio frame sync (11 set bits).
    return data.startswith(b"ID3") or (
        data[:1] == b"\xff" and len(data) > 1 and (data[1] & _SYNC_MASK) == _SYNC_MASK
    )


def _iso_media(data: bytes) -> bool:
    return data[4:8] == b"ftyp"


def _pdf(data: bytes) -> bool:
    # Readers accept the header anywhere in the first KiB.
    return b"%PDF-" in data[:1024]


_ZIP = _prefix(b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_OLE = _prefix(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
_OGG = _prefix(b"OggS")
_EBML = _prefix(b"\x1a\x45\xdf\xa3")
_ICO = _prefix(b"\x00\x00\x01\x00")

_SIGNATURES: dict[str, Callable[[bytes], bool]] = {
    "image/png": _prefix(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": _prefix(b"\xff\xd8\xff"),
    "image/jpg": _prefix(b"\xff\xd8\xff"),
    "image/gif": _prefix(b"GIF87a", b"GIF89a"),
    "image/webp": _riff(b"WEBP"),
    "image/bmp": _prefix(b"BM"),
    "image/tiff": _prefix(b"II*\x00", b"MM\x00*"),
    "image/x-icon": _ICO,
    "image/vnd.microsoft.icon": _ICO,
    "application/pdf": _pdf,
    "application/zip": _ZIP,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": _ZIP,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": _ZIP,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": _ZIP,
    "application/msword": _OLE,
    "application/vnd.ms-excel": _OLE,
    "application/vnd.ms-powerpoint": _OLE,
    "audio/mpeg": _mp3,
    "audio/mp3": _mp3,
    "audio/wav": _riff(b"WAVE"),
    "audio/x-wav": _riff(b"WAVE"),
    "audio/wave": _riff(b"WAVE"),
    "audio/ogg": _OGG,
    "video/ogg": _OGG,
    "application/ogg": _OGG,
    "audio/flac": _prefix(b"fLaC"),
    "audio/x-flac": _prefix(b"fLaC"),
    "video/mp4": _iso_media,
    "audio/mp4": _iso_media,
    "audio/x-m4a": _iso_media,
    "video/webm": _EBML,
    "audio/webm": _EBML,
}


def content_matches_mime(data: bytes, mime: str) -> bool:
    """Whether ``data`` starts like a file of type ``mime``; True when the type has no signature."""
    check = _SIGNATURES.get(mime.split(";", 1)[0].strip().lower())
    return True if check is None else check(data)
