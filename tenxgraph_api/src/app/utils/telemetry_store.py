"""In-memory telemetry store for observability.

Captures per-run streaming records (one per ``StreamChunk``) keyed by thread, so
the observability endpoint can reconstruct a trace (spans + events + token cost)
from the events a run actually emitted. This is process-local and best-effort —
it is not a durable telemetry backend; restart clears it.

A single instance is bound as an InjectQ singleton and written to by the graph
service during invoke/stream runs.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any


# Bounds so long-lived servers don't grow unbounded.
MAX_THREADS = 200
MAX_RUNS_PER_THREAD = 20
MAX_RECORDS_PER_RUN = 2000


class RunTrace:
    """A single graph run's captured records."""

    def __init__(self, run_id: str, thread_id: str, started_at: float) -> None:
        self.run_id = run_id
        self.thread_id = thread_id
        self.started_at = started_at
        self.finished_at: float | None = None
        self.status: str = "running"  # running | done | error | stopped
        self.records: deque[dict[str, Any]] = deque(maxlen=MAX_RECORDS_PER_RUN)

    def add(self, record: dict[str, Any]) -> None:
        self.records.append(record)


class TelemetryStore:
    """Thread-safe, in-memory store of run traces keyed by thread id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # thread_id -> ordered dict-like: run_id -> RunTrace (kept insertion order)
        self._threads: dict[str, dict[str, RunTrace]] = {}
        # No maxlen: a deque that drops its oldest id on append would leave that thread in the
        # dict for good. The eviction loop in start_run is the only bound.
        self._thread_order: deque[str] = deque()

    def start_run(self, thread_id: str, run_id: str, started_at: float) -> RunTrace:
        thread_id = str(thread_id)
        run_id = str(run_id)
        with self._lock:
            runs = self._threads.get(thread_id)
            if runs is None:
                runs = {}
                self._threads[thread_id] = runs
                if thread_id in self._thread_order:
                    self._thread_order.remove(thread_id)
                self._thread_order.append(thread_id)
                # Evict oldest threads beyond the cap.
                while len(self._threads) > MAX_THREADS and self._thread_order:
                    oldest = self._thread_order.popleft()
                    self._threads.pop(oldest, None)

            trace = RunTrace(run_id, thread_id, started_at)
            runs[run_id] = trace
            # Cap runs per thread (drop oldest by insertion order).
            while len(runs) > MAX_RUNS_PER_THREAD:
                oldest_run = next(iter(runs))
                runs.pop(oldest_run, None)
            return trace

    def record(self, thread_id: str, run_id: str, record: dict[str, Any]) -> None:
        thread_id = str(thread_id)
        run_id = str(run_id)
        with self._lock:
            runs = self._threads.get(thread_id)
            if not runs:
                return
            trace = runs.get(run_id)
            if trace:
                trace.add(record)

    def finish_run(
        self,
        thread_id: str,
        run_id: str,
        finished_at: float,
        status: str = "done",
    ) -> None:
        thread_id = str(thread_id)
        run_id = str(run_id)
        with self._lock:
            runs = self._threads.get(thread_id)
            if not runs:
                return
            trace = runs.get(run_id)
            if trace:
                trace.finished_at = finished_at
                trace.status = status

    def close_run(self, thread_id: str, run_id: str, finished_at: float, status: str) -> None:
        """Finish a run only if it is still open (a cancelled run, an unhandled error)."""
        thread_id = str(thread_id)
        run_id = str(run_id)
        with self._lock:
            trace = (self._threads.get(thread_id) or {}).get(run_id)
            if trace and trace.finished_at is None:
                trace.finished_at = finished_at
                trace.status = status

    def get_runs(self, thread_id: str) -> list[RunTrace]:
        """Runs for a thread, newest last (insertion order)."""
        with self._lock:
            runs = self._threads.get(str(thread_id))
            return list(runs.values()) if runs else []

    def get_latest_run(self, thread_id: str) -> RunTrace | None:
        runs = self.get_runs(thread_id)
        return runs[-1] if runs else None

    def get_run(self, thread_id: str, run_id: str) -> RunTrace | None:
        with self._lock:
            runs = self._threads.get(str(thread_id))
            return runs.get(str(run_id)) if runs else None

    def delete_thread(self, thread_id: str) -> None:
        """Completely clear a thread's traces from the in-memory telemetry cache."""
        thread_id = str(thread_id)
        with self._lock:
            self._threads.pop(thread_id, None)
            if thread_id in self._thread_order:
                self._thread_order.remove(thread_id)
