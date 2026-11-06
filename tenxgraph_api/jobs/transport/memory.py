"""In-process transport for tests and single-process use (one event loop).

Messages are kept in their wire form (``TransportMessage.to_dict``) and parsed on consume, like
a real broker, so format handling is exercised too.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections import deque
from collections.abc import Sequence
from typing import Any

from tenxgraph_api.jobs.core.models import PRIORITIES
from tenxgraph_api.jobs.transport.base import (
    BaseTransport,
    Delivery,
    TransportMessage,
    UnknownMessageFormat,
)


logger = logging.getLogger("tenxgraph_api.jobs")

Wire = dict[str, Any]


class MemoryTransport(BaseTransport):
    """Per-queue, per-priority FIFO deques. Unacked deliveries are kept until ``ack`` or
    ``requeue_unacked``."""

    def __init__(self) -> None:
        self._queues: dict[str, dict[str, deque[Wire]]] = {}
        self._pending: dict[int, Wire] = {}
        self._receipts = itertools.count(1)
        self._wakeup = asyncio.Event()
        self._closed = False
        self.dropped = 0

    def _lanes(self, queue: str) -> dict[str, deque[Wire]]:
        lanes = self._queues.get(queue)
        if lanes is None:
            lanes = {p: deque() for p in PRIORITIES}
            self._queues[queue] = lanes
        return lanes

    def _push(self, wire: Wire, *, front: bool = False) -> None:
        queue = str(wire.get("queue", "default"))
        priority = wire.get("p")
        lane = self._lanes(queue)[priority if priority in PRIORITIES else "normal"]
        if front:
            lane.appendleft(wire)
        else:
            lane.append(wire)
        self._wakeup.set()

    async def publish(self, message: TransportMessage) -> None:
        if self._closed:
            raise RuntimeError("transport is closed")
        self._push(message.to_dict())

    def publish_raw(self, wire: Wire) -> None:
        """Put a raw wire dict on the transport (tests: messages from other code versions)."""
        self._push(dict(wire))

    def _take(
        self, queues: Sequence[str], max_messages: int, levels: Sequence[str]
    ) -> list[Delivery]:
        out: list[Delivery] = []
        for priority in levels:
            for queue in queues:
                lane = self._lanes(queue)[priority]
                while lane and len(out) < max_messages:
                    wire = lane.popleft()
                    try:
                        message = TransportMessage.from_dict(wire)
                    except (UnknownMessageFormat, KeyError, TypeError, ValueError) as exc:
                        # Acked and dropped: the store republishes the run (next_publish_at).
                        self.dropped += 1
                        logger.warning("dropped unreadable jobs message %r: %s", wire, exc)
                        continue
                    receipt = next(self._receipts)
                    self._pending[receipt] = wire
                    out.append(Delivery(message=message, receipt=receipt))
        return out

    async def consume(
        self,
        queues: Sequence[str],
        *,
        max_messages: int,
        block: float = 0.0,
        priorities: Sequence[str] | None = None,
    ) -> list[Delivery]:
        if max_messages <= 0 or self._closed:
            return []
        levels = [p for p in PRIORITIES if priorities is None or p in priorities]
        if not levels:
            return []
        out = self._take(queues, max_messages, levels)
        if out or block <= 0:
            return out
        self._wakeup.clear()
        try:
            await asyncio.wait_for(self._wakeup.wait(), timeout=block)
        except TimeoutError:
            return []
        return self._take(queues, max_messages, levels)

    async def ack(self, deliveries: Sequence[Delivery]) -> None:
        for delivery in deliveries:
            self._pending.pop(delivery.receipt, None)

    def requeue_unacked(self) -> int:
        """Put every unacked delivery back at the front of its lane (the in-process stand-in for
        redelivery after a consumer died). Returns how many."""
        pending = list(self._pending.values())
        self._pending.clear()
        for wire in reversed(pending):
            self._push(wire, front=True)
        return len(pending)

    def size(self) -> int:
        """Messages waiting (not delivered)."""
        return sum(len(lane) for lanes in self._queues.values() for lane in lanes.values())

    def queue_names(self) -> list[str]:
        """Every queue a message was ever published to."""
        return list(self._queues)

    def unacked(self) -> int:
        return len(self._pending)

    async def aclose(self) -> None:
        self._closed = True
        self._wakeup.set()
