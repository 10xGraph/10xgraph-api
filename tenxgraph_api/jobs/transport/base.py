"""The transport contract.

A transport carries one thing: "run X is ready" (J3). It is a wake-up signal, never the
source of truth: the run store decides, through ``claim``, who runs a job. So a transport may
deliver a message twice, late, or after the run already finished; the worker claims, and a
failed claim is simply acked and skipped.

Rules every implementation follows (``tests/unit_tests/jobs/contract`` checks them):

- No delays. A transport delivers what it is given as soon as a consumer asks. Delayed and
  retrying runs stay in the run store until due; the sweeper then publishes them
  (``BaseRunStore.due_runs``). This keeps every transport small and makes Redis Streams,
  RabbitMQ and Kafka interchangeable.
- Priority inside a queue: ``consume`` returns ``high`` before ``normal`` before ``low``
  when several are waiting (strict in v1; fairness and reserved slots are worker policy,
  Sprint 3). Order inside one priority is publish order (best effort).
- At least once: a delivered message that is never acked comes back to some consumer later
  (Redis ``XAUTOCLAIM``, RabbitMQ redelivery). The memory transport, which lives in one
  process, gives unacked messages back with ``requeue_unacked``.
- Every message carries ``version`` (``FORMAT_VERSION``). A consumer that reads a version
  (or a body) it does not understand acks it, drops it and logs a warning; it never returns
  it from ``consume`` and never leaves it unacked (an unacked poison entry would come back
  forever through ``XAUTOCLAIM``). The run itself is safe in the store: its
  ``next_publish_at`` makes the sweeper publish it again later, in a format every pod reads.
- ``consume(..., priorities=[...])`` reads only those levels, still highest first, and leaves
  every other message untouched.
- ``consume`` blocks at most ``block`` seconds (0 = do not wait) and returns at most
  ``max_messages``; it never blocks forever.
- ``consume`` called after ``aclose``, or still waiting when ``aclose`` runs, returns ``[]``
  (it never raises because the transport closed). ``aclose`` never loses a delivered entry:
  one handed out by a read that ``aclose`` cut off stays pending for redelivery.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict


FORMAT_VERSION = 1


class UnknownMessageFormat(ValueError):
    """A message with a format version this code does not read."""


class TransportMessage(BaseModel):
    """The wake-up message. ``version`` is the wire format version."""

    model_config = ConfigDict(frozen=True)

    run_id: str
    queue: str = "default"
    priority: str = "normal"
    version: int = FORMAT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {"v": self.version, "run_id": self.run_id, "queue": self.queue, "p": self.priority}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TransportMessage:
        version = int(data.get("v", 0))
        if version != FORMAT_VERSION:
            raise UnknownMessageFormat(f"message format version {version} is not supported")
        return cls(
            run_id=str(data["run_id"]),
            queue=str(data.get("queue", "default")),
            priority=str(data.get("p", "normal")),
            version=version,
        )


class Delivery(BaseModel):
    """A received message. ``receipt`` is the transport's own handle for ``ack`` (a stream
    entry id, a delivery tag); callers treat it as opaque."""

    model_config = ConfigDict(frozen=True)

    message: TransportMessage
    receipt: Any


class BaseTransport(ABC):
    """Async transport. See the module docstring for the rules."""

    async def setup(self) -> None:  # noqa: B027 - optional hook
        """Create streams / groups / queues. Idempotent."""

    @abstractmethod
    async def publish(self, message: TransportMessage) -> None:
        """Send one message to ``message.queue`` at ``message.priority``."""

    async def publish_many(self, messages: Sequence[TransportMessage]) -> None:
        """Send several messages; override to batch."""
        for message in messages:
            await self.publish(message)

    @abstractmethod
    async def consume(
        self,
        queues: Sequence[str],
        *,
        max_messages: int,
        block: float = 0.0,
        priorities: Sequence[str] | None = None,
    ) -> list[Delivery]:
        """Up to ``max_messages`` messages from ``queues``, higher priority first.

        Waits at most ``block`` seconds when nothing is ready; returns ``[]`` on timeout.
        ``priorities`` (default: all) limits the read to messages of those priority levels;
        others stay where they are for the next call (the worker reads only ``high`` when only
        its reserved high-priority slots are free). An empty list reads nothing.
        """

    @abstractmethod
    async def ack(self, deliveries: Sequence[Delivery]) -> None:
        """Acknowledge deliveries so they are not delivered again. Acking twice is a no-op."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Close connections. Calling it twice is a no-op."""
