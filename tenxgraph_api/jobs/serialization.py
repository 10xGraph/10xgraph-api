"""JSON encoding shared by every run store.

One encoder, so every backend accepts and refuses the same values: ``dumps`` raises
``ValueNotStorable`` for NaN or Infinity (``allow_nan=False``), for any string (value or
object key, at any depth) holding a NUL character, and for values JSON cannot encode. Postgres
cannot store the first two (``jsonb`` refuses ``\\u0000`` and NaN), so the others refuse them
too instead of storing what one backend would lose. Stores call these helpers before any
write, so a refused value changes nothing.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from .errors import ValueNotStorable


if TYPE_CHECKING:
    from .models import AttemptOutcome, EventIn, NewRun

#: What a NUL character looks like in ``json.dumps`` output (``ensure_ascii`` escapes it).
_NUL_ESCAPE = "\\u0000"


def _has_nul(value: Any) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in value.items())
    if isinstance(value, list | tuple):
        return any(_has_nul(v) for v in value)
    return False


def dumps(value: Any, *, field: str) -> str:
    """Compact JSON text of ``value``; raises ``ValueNotStorable(field, reason)``."""
    try:
        text = json.dumps(value, separators=(",", ":"), allow_nan=False)
    except ValueError as exc:
        raise ValueNotStorable(field, "NaN and Infinity are not valid JSON") from exc
    except TypeError as exc:
        raise ValueNotStorable(field, f"not JSON serializable ({exc})") from exc
    # Every NUL shows up as the escape; only then walk the value (a literal backslash followed
    # by "u0000" also matches the text, so the walk decides).
    if _NUL_ESCAPE in text and _has_nul(value):
        raise ValueNotStorable(field, "strings must not contain the NUL character (\\x00)")
    return text


def check(value: Any, *, field: str) -> None:
    """Raise ``ValueNotStorable`` unless ``dumps(value)`` would succeed."""
    dumps(value, field=field)


def check_new_run(new: NewRun) -> None:
    """The JSON fields of a new run."""
    check(new.kwargs, field="kwargs")
    check(new.identity, field="identity")
    check(new.trigger, field="trigger")


def check_outcome(outcome: AttemptOutcome) -> None:
    """The JSON fields an attempt outcome writes."""
    check(outcome.result, field="result")
    if outcome.error is not None:
        check(outcome.error, field="error")


def check_events(events: Iterable[EventIn], progress: Any = None) -> None:
    """Event types and data, and the progress snapshot, of one flush."""
    for event in events:
        if "\x00" in event.type:
            raise ValueNotStorable(
                "event.type", "strings must not contain the NUL character (\\x00)"
            )
        check(event.data, field="event.data")
    if progress is not None:
        check(progress, field="progress")
