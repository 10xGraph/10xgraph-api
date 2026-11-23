"""Leading-byte checks for uploaded files.

The signature table lives in core (``tenxgraph.storage.media.security.validate_magic_bytes``).
``content_matches_mime`` is kept as an import name for it.
"""

from tenxgraph.storage.media.security import validate_magic_bytes as content_matches_mime


__all__ = ["content_matches_mime"]
