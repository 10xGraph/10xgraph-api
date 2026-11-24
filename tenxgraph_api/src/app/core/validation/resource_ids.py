"""Thread and message ids in the type the bound id generator makes.

Ids arrive as strings in paths, query strings and WebSocket frames. Core stores them as the
generator makes them (``str`` for the default uuid generator, ``int`` for Snowflake and the
other integer generators) and no longer converts numeric strings. :func:`to_core_id` is the one
place the API does that, for every route that passes a thread or message id to core. It is
idempotent, so a layer below a route may call it again.
"""

from __future__ import annotations

from typing import Any

from injectq import InjectQ

from tenxgraph_api.src.app.core.validation.request_config import ClientInputError


# Largest id the integer columns of ``PgCheckpointer`` hold, by id type.
_MAX_INT_ID = {"int": 2**31 - 1, "bigint": 2**63 - 1}


def generator_id_type() -> str:
    """``"string"``, ``"int"`` or ``"bigint"``: the id type of the bound id generator."""
    bound = InjectQ.get_instance().try_get("generated_id_type", "string")
    plain = str(getattr(bound, "value", bound))
    if plain == "integer":
        return "int"
    return plain if plain in _MAX_INT_ID else "string"


def to_core_id(value: Any, field: str = "thread_id") -> str | int | None:
    """``value`` as the id type of the id generator; ``None`` stays ``None``.

    A string generator takes strings (an integer is written out as text). An integer generator
    takes integers, and a numeric string is converted.

    Raises:
        ClientInputError: The value is not an integer in range for an integer generator (422).
    """
    if value is None or isinstance(value, bool):
        return value
    id_type = generator_id_type()
    if id_type == "string":
        return value if isinstance(value, str) else str(value)
    limit = _MAX_INT_ID.get(id_type, _MAX_INT_ID["bigint"])
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        number = int(value.strip())
    else:
        raise ClientInputError(f"{field} must be an integer")
    if not 0 <= number <= limit:
        raise ClientInputError(f"{field} must be an integer between 0 and {limit}")
    return number
