"""Control-flow signals a task raises to steer its own run."""

from __future__ import annotations

from typing import Any


class RetryAfter(Exception):
    """Run again in ``seconds`` without using a retry (for a provider 429).

    The run goes back to ``queued`` with ``available_at = now + seconds``. A run may defer at
    most ``MAX_DEFERRALS`` (50) times; the next ``RetryAfter`` fails it.
    """

    def __init__(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("RetryAfter seconds must be >= 0")
        super().__init__(f"retry after {seconds} s")
        self.seconds = float(seconds)


class Fail(Exception):
    """Fail the run now, whether or not retries are left."""

    def __init__(self, message: str, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.data = data
