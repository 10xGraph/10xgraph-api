"""Seeded random-operation test of the run state machine (hypothesis is not a dev dependency).

Two workers share one memory store and transport. Each step picks a random operation:
enqueue, cancel, poll, tick, sweep, let tasks run, move the clock, stall a worker's heartbeat,
crash a worker, or fire a stale write. After every step the invariants are checked:

- at most one task body EXECUTING per run at any moment (checked inside the body, not only
  at the writes);
- at most one live attempt per run across both workers, and it matches the store's attempt
  and worker id;
- every observed state change is a legal edge, ``attempt`` only grows (by one per claim);
- terminal states are final (state, result and error never change again);
- a stale attempt's writes (old attempt or wrong worker) are rejected;
- concurrency counters never go negative, never exceed the cap and equal the leased runs
  holding a slot.

At the end every run reaches a terminal state once the workers recover and time moves on.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from tenxgraph_api.jobs import Fail, RetryAfter, TaskContext, task
from tenxgraph_api.jobs.core.backoff import Backoff
from tenxgraph_api.jobs.core.models import (
    LEASED_STATES,
    TERMINAL_STATES,
    AttemptOutcome,
    EventIn,
    HeldLease,
    RunState,
)
from tenxgraph_api.jobs.store.base import slot_name
from tenxgraph_api.jobs.testing import jobs_harness
from tenxgraph_api.jobs.worker.worker import Worker


pytestmark = pytest.mark.asyncio

CAP = 2
LEASE_TTL = 30.0

S = RunState
LEGAL = {
    (S.QUEUED, S.RUNNING),
    (S.QUEUED, S.CANCELLED),
    (S.RETRYING, S.RUNNING),
    (S.RETRYING, S.CANCELLED),
    (S.RUNNING, S.SUCCEEDED),
    (S.RUNNING, S.FAILED),
    (S.RUNNING, S.RETRYING),
    (S.RUNNING, S.QUEUED),
    (S.RUNNING, S.CANCELLING),
    (S.CANCELLING, S.CANCELLED),
}


EXECUTING: dict[str, int] = {}
VIOLATIONS: list[str] = []


async def behave(seed: int, ctx: TaskContext) -> int:
    running = EXECUTING.get(ctx.run_id, 0)
    if running:
        VIOLATIONS.append(f"{ctx.run_id} attempt {ctx.attempt} started while {running} ran")
    EXECUTING[ctx.run_id] = running + 1
    try:
        return await _behave(seed, ctx)
    finally:
        EXECUTING[ctx.run_id] -= 1


async def _behave(seed: int, ctx: TaskContext) -> int:
    rng = random.Random(seed * 7919 + ctx.attempt)  # noqa: S311 - test data
    for _ in range(rng.randint(0, 3)):
        ctx.emit.progress("working", percent=rng.random() * 100)
        await asyncio.sleep(0)
    roll = rng.random()
    if roll < 0.15:
        raise RuntimeError("flaky")
    if roll < 0.2:
        raise Fail("bad input")
    if roll < 0.3 and ctx.attempt < 4:
        raise RetryAfter(rng.choice([0, 1, 5]))
    if roll < 0.32:
        await asyncio.sleep(0.03)  # past sm_timed's timeout
    return ctx.attempt


@task(retries=2, backoff=Backoff(base=1, cap=4, jitter=False), max_lost=1)
async def sm_free(seed: int, ctx: TaskContext) -> int:
    return await behave(seed, ctx)


@task(
    retries=1,
    backoff=2,
    concurrency=CAP,
    concurrency_key="group",
    max_lost=2,
)
async def sm_capped(seed: int, group: str, ctx: TaskContext) -> int:
    return await behave(seed, ctx)


@task(retries=1, backoff=1, timeout=0.01)
async def sm_timed(seed: int, ctx: TaskContext) -> int:
    return await behave(seed, ctx)


CAPS = {"sm_capped": CAP}


class Checker:
    def __init__(self, jobs, workers):
        self.jobs = jobs
        self.workers = workers
        self.seen: dict = {}
        self.final: dict = {}
        self.stale_checks = 0

    def check(self, label: str, *, compound: bool = False) -> None:
        """``compound``: the step may have made several transitions (a whole drain), so only
        the end state is checked, not the edge."""
        assert not VIOLATIONS, (label, VIOLATIONS)
        assert all(n <= 1 for n in EXECUTING.values()), (label, EXECUTING)
        runs = self.jobs.store.snapshot()
        for run_id, run in runs.items():
            prev = self.seen.get(run_id)
            if prev is not None:
                assert run.attempt >= prev.attempt, (label, run_id)
                if run.state != prev.state and not compound:
                    assert (prev.state, run.state) in LEGAL, (label, prev.state, run.state)
                if not compound and run.state == S.RUNNING and prev.state in (S.QUEUED, S.RETRYING):
                    assert run.attempt == prev.attempt + 1, label
            if run_id in self.final:
                state, result, error = self.final[run_id]
                assert (run.state, run.result, run.error) == (state, result, error), label
            elif run.state in TERMINAL_STATES:
                self.final[run_id] = (run.state, run.result, run.error)
            if run.state in LEASED_STATES:
                assert run.worker_id is not None and run.lease_expires_at is not None
            else:
                assert run.slot is None, (label, run_id)
            self.seen[run_id] = run

        # One live attempt per run, matching the store.
        live: dict[str, list] = {}
        for worker in self.workers:
            for run_id, attempt in worker.inflight.items():
                if attempt.fenced_out or attempt.finished:
                    continue
                run = runs[run_id]
                if run.state in LEASED_STATES and run.attempt == attempt.run.attempt:
                    assert run.worker_id == worker.worker_id, label
                    live.setdefault(run_id, []).append(worker.worker_id)
        assert all(len(v) == 1 for v in live.values()), (label, live)

        # Counters.
        counters = self.jobs.store.counters()
        holding: dict[str, int] = {}
        for run in runs.values():
            if run.slot is not None:
                holding[run.slot] = holding.get(run.slot, 0) + 1
        for name, value in counters.items():
            task_name = name.split("\x1f", 1)[0]
            assert 0 <= value <= CAPS.get(task_name, 10**9), (label, name, value)
            assert value == holding.get(name, 0), (label, name, value, holding)

    async def stale_write(self, rng: random.Random) -> None:
        runs = [r for r in self.jobs.store.snapshot().values() if r.attempt >= 1]
        if not runs:
            return
        run = rng.choice(runs)
        before = self.jobs.store.snapshot()[run.run_id]
        store = self.jobs.store
        old = run.attempt - 1
        wid = run.worker_id or "w1"
        assert (
            await store.transition(
                run.run_id,
                attempt=old,
                worker_id=wid,
                outcome=AttemptOutcome(kind="succeeded", result="stale"),
            )
            is None
        )
        assert not await store.append_events(
            run.run_id, attempt=old, worker_id=wid, events=[EventIn(type="log", at=0.0)]
        )
        assert (
            await store.renew_leases(
                "intruder", [HeldLease(run_id=run.run_id, attempt=run.attempt)], lease_ttl=1
            )
            == {}
        )
        assert (
            await store.transition(
                run.run_id,
                attempt=run.attempt,
                worker_id="intruder",
                outcome=AttemptOutcome(kind="failed"),
            )
            is None
        )
        assert self.jobs.store.snapshot()[run.run_id] == before
        self.stale_checks += 1


async def enqueue_random(rng: random.Random, n: int) -> None:
    ident = {"user_id": f"u{rng.randint(1, 3)}"}
    choice = rng.random()
    opts = {"identity": ident, "priority": rng.choice(["high", "normal", "low"])}
    if rng.random() < 0.2:
        opts["delay"] = rng.choice([1, 10, 60])
    if choice < 0.45:
        await sm_capped.options(**opts).enqueue(seed=n, group=rng.choice(["a", "b"]))
    elif choice < 0.9:
        await sm_free.options(**opts).enqueue(seed=n)
    else:
        await sm_timed.options(**opts).enqueue(seed=n)


async def step(rng, jobs, workers, checker, n):
    op = rng.choices(
        [
            "enqueue",
            "cancel",
            "poll",
            "tick",
            "sweep",
            "run",
            "advance",
            "stall",
            "crash",
            "stale",
        ],
        weights=[14, 3, 14, 10, 6, 16, 6, 2, 1, 4],
    )[0]
    worker = rng.choice(workers)
    if op == "enqueue":
        await enqueue_random(rng, n)
    elif op == "cancel":
        runs = list(jobs.store.snapshot().values())
        if runs:
            await jobs.store.request_cancel(rng.choice(runs).run_id)
    elif op == "poll":
        await worker.poll_once()
    elif op == "tick":
        await worker.tick()
    elif op == "sweep":
        await jobs.sweep()
    elif op == "run":
        for _ in range(rng.randint(1, 6)):
            await asyncio.sleep(0)
    elif op == "advance":
        # Time passes in small steps; each worker's timer checks its local lease deadline on
        # the way, as the tick loop does in real time (store-free, so it works while stalled).
        remaining = rng.choice([0.5, 1, 2, 5, LEASE_TTL / 2, LEASE_TTL + 1])
        while remaining > 0:
            step_size = min(remaining, LEASE_TTL / 20)
            jobs.clock.advance(step_size)
            remaining -= step_size
            for w in workers:
                w.check_deadlines()
            await asyncio.sleep(0)
    elif op == "stall":
        worker.heartbeats = not worker.heartbeats
    elif op == "crash":
        await worker.abort()
        worker.resume()
    elif op == "stale":
        await checker.stale_write(rng)
    return op


@pytest.mark.parametrize("seed", range(30))
async def test_random_operations_keep_invariants(seed):
    rng = random.Random(seed)  # noqa: S311 - test data
    EXECUTING.clear()
    VIOLATIONS.clear()
    async with jobs_harness(lease_ttl=LEASE_TTL, concurrency=4) as jobs:
        w2 = Worker(
            jobs.runtime,
            worker_id="worker-2",
            concurrency=3,
            queues=jobs.transport.queue_names,
            monotonic=jobs.clock,
        )
        workers = [jobs.worker, w2]
        checker = Checker(jobs, workers)
        for n in range(160):
            op = await step(rng, jobs, workers, checker, n)
            checker.check(f"seed={seed} step={n} op={op}")

        # Recovery: w2 dies for good, the harness worker heals, time moves on.
        await w2.abort()
        jobs.worker.heartbeats = True
        jobs.worker.resume()
        for _ in range(60):
            await jobs.drain(follow_delays=True)
            checker.check(f"seed={seed} drain", compound=True)
            pending = [r for r in jobs.store.snapshot().values() if r.state not in TERMINAL_STATES]
            if not pending:
                break
            jobs.clock.advance(LEASE_TTL + 1)
        runs = jobs.store.snapshot()
        assert runs, "the sequence created no runs"
        assert all(r.state in TERMINAL_STATES for r in runs.values()), [
            (r.run_id, r.state) for r in runs.values() if r.state not in TERMINAL_STATES
        ]
        assert all(v == 0 for v in jobs.store.counters().values())
        for run in runs.values():
            assert run.failure_count <= run.max_retries + 1
            assert run.lost_count <= run.max_lost + 1
            if run.state == S.SUCCEEDED:
                assert run.result == run.attempt
            assert await jobs.store.concurrency_count(run.task, run.concurrency_key) == 0
        assert slot_name("sm_capped", "a")  # counters are keyed per (task, key)
