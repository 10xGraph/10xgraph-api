from abc import ABC, abstractmethod
from dataclasses import dataclass


# Retry-After (seconds) sent with the 503 answered while the backend cannot be reached.
UNAVAILABLE_RETRY_AFTER_SECONDS = 5


@dataclass(frozen=True)
class RateLimitDecision:
    """Result of a single rate-limit check.

    ``unavailable`` marks a denial caused by the backend (it could not be reached), not by the
    limit: the middleware then answers 503 ``RATE_LIMIT_UNAVAILABLE`` instead of 429, with
    ``reset_after`` as its ``Retry-After``. Backends that never set it keep working unchanged.
    """

    allowed: bool
    remaining: int
    reset_after: int
    unavailable: bool = False

    @classmethod
    def backend_unavailable(
        cls, retry_after: int = UNAVAILABLE_RETRY_AFTER_SECONDS
    ) -> "RateLimitDecision":
        """A denial because the backend failed; answered 503 with ``Retry-After``."""
        return cls(allowed=False, remaining=0, reset_after=retry_after, unavailable=True)


class BaseRateLimitBackend(ABC):
    """Abstract base class for rate-limit backends.

    Users can implement this class and bind an instance into InjectQ when they
    need a backend other than the built-in memory or Redis implementations. A backend that
    cannot reach its store and must not let the request through returns
    :meth:`RateLimitDecision.backend_unavailable`.
    """

    @abstractmethod
    async def check(self, key: str, *, limit: int, window: int) -> RateLimitDecision:
        """Atomically check and record a request for *key*."""

    @abstractmethod
    async def close(self) -> None:
        """Release resources held by the backend, if any."""
