"""The worker tick: lease renewals with the cancel watch, local deadlines, progress flushes.

``LeaseKeeper`` is a mixin of ``Worker``. Store errors in a tick are logged, never raised.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from tenxgraph_api.jobs.core.models import HeldLease
from tenxgraph_api.jobs.errors import ValueNotStorable
from tenxgraph_api.jobs.store.base import BaseRunStore

from .attempt import Attempt
from .hooks_queue import ProgressHooks
from .logs import log_run


logger = logging.getLogger("tenxgraph_api.jobs")


class LeaseKeeper:
    """Mixin of ``Worker``: keeps the leases of running attempts and flushes their progress."""

    store: BaseRunStore
    worker_id: str
    lease_ttl: float
    lease_margin: float
    flush_interval: float
    heartbeats: bool
    _monotonic: Callable[[], float]
    _inflight: dict[str, Attempt]
    _hooks: ProgressHooks

    if TYPE_CHECKING:

        def _stop(self, attempt: Attempt, reason: str) -> None: ...

        def _lose(self, attempt: Attempt, why: str) -> None: ...

    async def tick(self) -> None:
        """One batched renewal (heartbeat plus cancel watch), the deadline check, due flushes."""
        held = [a for a in self._inflight.values() if not a.fenced_out and not a.written]
        if self.heartbeats and held:
            sent_at = self._monotonic()
            try:
                status = await self.store.renew_leases(
                    self.worker_id,
                    [HeldLease(run_id=a.run_id, attempt=a.run.attempt) for a in held],
                    lease_ttl=self.lease_ttl,
                )
            except Exception:
                logger.warning("jobs lease renewal failed; will retry", exc_info=True)
            else:
                self._apply_renewals(held, status, sent_at)
        self.check_deadlines()
        if not self.heartbeats:
            return
        for attempt in held:
            if attempt.finished or attempt.fenced_out or not attempt.emitter.dirty:
                continue
            try:
                await self._flush(attempt)
            except Exception:
                logger.warning("progress flush of run %s failed", attempt.run_id, exc_info=True)

    def _apply_renewals(self, held: list[Attempt], status: dict[str, bool], sent_at: float) -> None:
        """Move each renewed deadline; lose a missing lease; stop a run whose cancel is set."""
        for attempt in held:
            if attempt.fenced_out or attempt.written:
                continue
            if attempt.run_id not in status:
                # A finishing attempt's outcome write may have landed: its write loop decides.
                if not attempt.finished:
                    self._lose(attempt, "lease renewal matched nothing")
                continue
            attempt.lease_deadline = sent_at + self.lease_ttl - self.lease_margin
            if status[attempt.run_id] and attempt.stop_reason is None and not attempt.finished:
                self._stop(attempt, "cancel")

    def check_deadlines(self) -> None:
        """Lose every attempt not renewed for ``lease_ttl - lease_margin`` (no store call)."""
        now = self._monotonic()
        for attempt in list(self._inflight.values()):
            if not attempt.fenced_out and not attempt.written and now >= attempt.lease_deadline:
                self._lose(attempt, "no lease renewal within lease_ttl")

    async def _flush(self, attempt: Attempt, *, force: bool = False) -> bool:  # noqa: PLR0911
        """Write buffered progress (fenced). False when the lease is gone."""
        async with attempt.flush_lock:
            if attempt.fenced_out:
                return False
            now = self._monotonic()
            if now >= attempt.lease_deadline:
                self._lose(attempt, "lease deadline passed before a progress flush")
                return False
            if not force and now - attempt.last_flush < self.flush_interval:
                return True
            taken = attempt.emitter.take()
            if taken is None:
                return True
            events, snapshot = taken
            attempt.last_flush = now
            try:
                ok = await self.store.append_events(
                    attempt.run_id,
                    attempt=attempt.run.attempt,
                    worker_id=self.worker_id,
                    events=events,
                    progress=snapshot,
                )
            except ValueNotStorable as exc:
                # Nothing was written and retrying cannot help: drop this flush.
                log_run(
                    logger,
                    logging.WARNING,
                    "progress dropped (not storable)",
                    attempt.run,
                    field=exc.field,
                )
                return True
            if not ok:
                self._lose(attempt, "progress flush matched nothing")
                return False
        for event in events:
            self._hooks.put(
                attempt.run,
                {"type": event.type, "at": event.at, "attempt": attempt.run.attempt, **event.data},
            )
        return True
