"""Retry backoff."""

from __future__ import annotations

import random
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, model_validator


_MAX_EXPONENT = 64


class Backoff(BaseModel):
    """Exponential backoff: ``min(cap, base * 2 ** (n - 1))`` before retry ``n`` (1-based).

    With ``jitter`` the delay is drawn from ``[delay / 2, delay]`` so retries of many runs do
    not land together. Default: 2 s base, 10 min cap, jitter on.
    """

    model_config = ConfigDict(frozen=True)

    base: float = 2.0
    cap: float = 600.0
    jitter: bool = True

    @model_validator(mode="after")
    def _non_negative(self) -> Backoff:
        if self.base < 0 or self.cap < 0:
            raise ValueError("backoff base and cap must be >= 0")
        return self

    def delay(self, failure_number: int) -> float:
        exponent = max(0, failure_number - 1)
        raw = self.cap if exponent >= _MAX_EXPONENT else min(self.cap, self.base * 2**exponent)
        if self.jitter and raw > 0:
            return random.uniform(raw / 2, raw)  # noqa: S311 - jitter, not security
        return raw


BackoffSpec = Backoff | float | int | Callable[[int], float] | None


def to_backoff(spec: BackoffSpec) -> Callable[[int], float]:
    """A ``failure_number -> seconds`` function from ``@task(backoff=...)``.

    ``None``: the default ``Backoff()``. A number: that many seconds every time. A ``Backoff``
    or a callable: used as is.
    """
    if spec is None:
        return Backoff().delay
    if isinstance(spec, Backoff):
        return spec.delay
    if isinstance(spec, bool):
        raise TypeError("backoff must be a Backoff, a number of seconds or a callable")
    if isinstance(spec, int | float):
        if spec < 0:
            raise ValueError("backoff seconds must be >= 0")
        fixed = float(spec)
        return lambda _n: fixed
    if callable(spec):
        return spec
    raise TypeError("backoff must be a Backoff, a number of seconds or a callable")
