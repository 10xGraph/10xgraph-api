"""The run store contract (BaseRunStore docstring). Runs against every factory in conftest."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tenxgraph_api.jobs.core.models import (
    EVENT_LOG_CAP,
    AttemptOutcome,
    CancelStatus,
    ClaimStatus,
    EventIn,
    HeldLease,
    NewRun,
    RunFilter,
    RunState,
)
from tenxgraph_api.jobs.errors import ValueNotStorable


pytestmark = pytest.mark.asyncio

TTL = 0.2
W1 = "worker-1"
W2 = "worker-2"


def uid(prefix: str = "r") -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def new_run(task: str | None = None, **over) -> NewRun:
    data = {
        "run_id": uid(),
        "task": task or uid("task"),
        "kwargs": {"x": 1},
        "identity": {"user_id": "u1"},
        "owner_user_id": "u1",
        "retention": 60.0,
    }
    data.update(over)
    return NewRun(**data)


async def claimed(sut, **over):
    created = await sut.store.create_run(new_run(**over))
    result = await sut.store.claim(created.run.run_id, worker_id=W1, lease_ttl=TTL)
    assert result.status == ClaimStatus.CLAIMED
    return result.run


# ---------------------------------------------------------------------- create and read


async def test_create_and_get(sut):
    now = await sut.store.now()
    res = await sut.store.create_run(new_run())
    assert res.created and res.due and not res.replaced
    run = res.run
    assert run.state == RunState.QUEUED
    assert run.attempt == 0
    assert run.published_at is None
    assert run.next_publish_at == run.available_at
    assert run.tenant == ""
    assert now <= run.available_at <= await sut.store.now()
    assert run.enqueued_at == run.available_at
    got = await sut.store.get_run(run.run_id)
    assert got == run
    assert await sut.store.get_run(uid()) is None


async def test_delay_and_at(sut):
    res = await sut.store.create_run(new_run(delay=100.0))
    assert res.created and not res.due
    assert res.run.available_at == pytest.approx(res.run.enqueued_at + 100.0, abs=0.05)
    at = (await sut.store.now()) + 50.0
    res2 = await sut.store.create_run(new_run(at=at))
    assert res2.run.available_at == pytest.approx(at)
    assert not res2.due


async def test_returned_records_are_copies(sut):
    run = (await sut.store.create_run(new_run())).run
    run.kwargs["x"] = 999
    run.state = RunState.FAILED
    got = await sut.store.get_run(run.run_id)
    assert got.kwargs == {"x": 1}
    assert got.state == RunState.QUEUED


async def test_idempotency_keep(sut):
    task = uid("task")
    first = await sut.store.create_run(new_run(task, idempotency_key="k"))
    again = await sut.store.create_run(new_run(task, idempotency_key="k", kwargs={"x": 2}))
    assert not again.created and not again.replaced and not again.due
    assert again.run.run_id == first.run.run_id
    assert again.run.kwargs == {"x": 1}
    other_owner = await sut.store.create_run(
        new_run(task, idempotency_key="k", owner_user_id="u2", identity={"user_id": "u2"})
    )
    assert other_owner.created
    other_task = await sut.store.create_run(new_run(idempotency_key="k"))
    assert other_task.created


async def test_idempotency_replace_only_while_queued(sut):
    task = uid("task")
    first = await sut.store.create_run(new_run(task, idempotency_key="k"))
    rep = await sut.store.create_run(
        new_run(task, idempotency_key="k", kwargs={"x": 2}), key_mode="replace"
    )
    assert rep.replaced and not rep.created and rep.due
    assert rep.run.run_id == first.run.run_id
    assert (await sut.store.get_run(first.run.run_id)).kwargs == {"x": 2}
    await sut.store.claim(first.run.run_id, worker_id=W1, lease_ttl=TTL)
    late = await sut.store.create_run(
        new_run(task, idempotency_key="k", kwargs={"x": 3}), key_mode="replace"
    )
    assert not late.replaced and not late.created
    assert (await sut.store.get_run(first.run.run_id)).kwargs == {"x": 2}


async def test_list_filters_order_and_cursor(sut):
    task = uid("task")
    ids = []
    for _ in range(7):
        ids.append((await sut.store.create_run(new_run(task))).run.run_id)
        await sut.advance(0.01)
    other = await sut.store.create_run(
        new_run(task, owner_user_id="u2", identity={"user_id": "u2"})
    )
    await sut.store.claim(ids[0], worker_id=W1, lease_ttl=TTL)

    page = await sut.store.list_runs(RunFilter(task=task, owner_user_id="u1"), limit=3)
    seen = [r.run_id for r in page.items]
    while page.next_cursor:
        page = await sut.store.list_runs(
            RunFilter(task=task, owner_user_id="u1"), limit=3, cursor=page.next_cursor
        )
        seen += [r.run_id for r in page.items]
    assert seen == list(reversed(ids))

    running = await sut.store.list_runs(RunFilter(task=task, state="running"))
    assert [r.run_id for r in running.items] == [ids[0]]
    both = await sut.store.list_runs(RunFilter(task=task, state=("running", "queued")))
    assert len(both.items) == 8
    mine = await sut.store.list_runs(RunFilter(task=task, owner_user_id="u2"))
    assert [r.run_id for r in mine.items] == [other.run.run_id]
    with pytest.raises(ValueError):
        await sut.store.list_runs(RunFilter(task=task), cursor="not-a-cursor")


# ---------------------------------------------------------------------- claim and leases


async def test_claim_cas(sut):
    run = (await sut.store.create_run(new_run())).run
    now = await sut.store.now()
    res = await sut.store.claim(run.run_id, worker_id=W1, lease_ttl=TTL)
    assert res.status == ClaimStatus.CLAIMED
    assert res.run.state == RunState.RUNNING
    assert res.run.attempt == 1
    assert res.run.worker_id == W1
    assert res.run.lease_expires_at == pytest.approx(now + TTL, abs=0.05)
    again = await sut.store.claim(run.run_id, worker_id=W2, lease_ttl=TTL)
    assert again.status == ClaimStatus.NOT_READY
    assert (await sut.store.claim(uid(), worker_id=W1, lease_ttl=TTL)).status == (
        ClaimStatus.NOT_FOUND
    )


async def test_claim_waits_for_available_at(sut):
    run = (await sut.store.create_run(new_run(delay=0.1))).run
    assert (await sut.store.claim(run.run_id, worker_id=W1, lease_ttl=TTL)).status == (
        ClaimStatus.NOT_READY
    )
    await sut.advance(0.15)
    assert (await sut.store.claim(run.run_id, worker_id=W1, lease_ttl=TTL)).status == (
        ClaimStatus.CLAIMED
    )


async def test_concurrency_cap_counter(sut):
    task = uid("task")
    a = (await sut.store.create_run(new_run(task, concurrency_key="t1"))).run
    b = (await sut.store.create_run(new_run(task, concurrency_key="t1"))).run
    c = (await sut.store.create_run(new_run(task, concurrency_key="t2"))).run
    ok = await sut.store.claim(a.run_id, worker_id=W1, lease_ttl=TTL, concurrency_limit=1)
    assert ok.status == ClaimStatus.CLAIMED
    assert await sut.store.concurrency_count(task, "t1") == 1
    before = await sut.store.now()
    capped = await sut.store.claim(b.run_id, worker_id=W1, lease_ttl=TTL, concurrency_limit=1)
    assert capped.status == ClaimStatus.CAPPED
    assert capped.run.state == RunState.QUEUED
    assert capped.run.cap_waits == 1
    assert capped.run.next_publish_at == capped.run.available_at
    assert capped.run.available_at == pytest.approx(before + 1.0, abs=0.05)
    other = await sut.store.claim(c.run_id, worker_id=W1, lease_ttl=TTL, concurrency_limit=1)
    assert other.status == ClaimStatus.CLAIMED

    done = await sut.store.transition(
        a.run_id, attempt=1, worker_id=W1, outcome=AttemptOutcome(kind="succeeded", result=1)
    )
    assert done.slot is None
    assert await sut.store.concurrency_count(task, "t1") == 0
    await sut.advance(1.05)
    later = await sut.store.claim(b.run_id, worker_id=W1, lease_ttl=TTL, concurrency_limit=1)
    assert later.status == ClaimStatus.CLAIMED
    assert later.run.cap_waits == 0


async def test_cap_wait_grows(sut):
    task = uid("task")
    a = (await sut.store.create_run(new_run(task))).run
    b = (await sut.store.create_run(new_run(task))).run
    await sut.store.claim(a.run_id, worker_id=W1, lease_ttl=10, concurrency_limit=1)
    waits = []
    for _ in range(3):
        now = await sut.store.now()
        res = await sut.store.claim(b.run_id, worker_id=W1, lease_ttl=10, concurrency_limit=1)
        assert res.status == ClaimStatus.CAPPED
        waits.append(round(res.run.available_at - now))
        await sut.advance(res.run.available_at - now + 0.01)
    assert waits == [1, 2, 4]


async def test_renew_is_fenced(sut):
    run = await claimed(sut)
    await sut.advance(0.1)
    now = await sut.store.now()
    status = await sut.store.renew_leases(
        W1, [HeldLease(run_id=run.run_id, attempt=1)], lease_ttl=TTL
    )
    assert status == {run.run_id: False}
    assert (await sut.store.get_run(run.run_id)).lease_expires_at == pytest.approx(
        now + TTL, abs=0.05
    )
    assert (
        await sut.store.renew_leases(W2, [HeldLease(run_id=run.run_id, attempt=1)], lease_ttl=TTL)
        == {}
    )
    assert (
        await sut.store.renew_leases(W1, [HeldLease(run_id=run.run_id, attempt=2)], lease_ttl=TTL)
        == {}
    )


async def test_renew_reports_cancel(sut):
    run = await claimed(sut)
    res = await sut.store.request_cancel(run.run_id)
    assert res.status == CancelStatus.REQUESTED
    assert res.run.state == RunState.CANCELLING
    status = await sut.store.renew_leases(
        W1, [HeldLease(run_id=run.run_id, attempt=1)], lease_ttl=TTL
    )
    assert status == {run.run_id: True}


# ---------------------------------------------------------------------- events


async def test_events_seq_cursor_and_fencing(sut):
    run = await claimed(sut)
    events = [EventIn(type="log", at=1.0, data={"i": i}) for i in range(5)]
    assert await sut.store.append_events(
        run.run_id, attempt=1, worker_id=W1, events=events, progress={"percent": 10.0}
    )
    got = await sut.store.read_events(run.run_id)
    assert [e.seq for e in got] == [1, 2, 3, 4, 5]
    assert all(e.attempt == 1 for e in got)
    assert [e.seq for e in await sut.store.read_events(run.run_id, after=3)] == [4, 5]
    assert len(await sut.store.read_events(run.run_id, limit=2)) == 2
    assert (await sut.store.get_run(run.run_id)).progress == {"percent": 10.0}
    assert not await sut.store.append_events(
        run.run_id, attempt=0, worker_id=W1, events=events, progress={"percent": 99.0}
    )
    assert not await sut.store.append_events(run.run_id, attempt=1, worker_id=W2, events=events)
    assert (await sut.store.get_run(run.run_id)).progress == {"percent": 10.0}
    assert len(await sut.store.read_events(run.run_id)) == 5


async def test_event_log_cap(sut):
    run = await claimed(sut)
    batch = [EventIn(type="log", at=1.0, data={"i": i}) for i in range(EVENT_LOG_CAP + 10)]
    assert await sut.store.append_events(run.run_id, attempt=1, worker_id=W1, events=batch)
    kept = []
    cursor = 0
    while True:
        page = await sut.store.read_events(run.run_id, after=cursor, limit=1000)
        if not page:
            break
        kept += page
        cursor = page[-1].seq
    assert len(kept) == EVENT_LOG_CAP
    assert kept[0].seq == 11
    assert kept[-1].seq == EVENT_LOG_CAP + 10


# ---------------------------------------------------------------------- transitions


@pytest.mark.parametrize(
    ("outcome", "state"),
    [
        (AttemptOutcome(kind="succeeded", result={"ok": True}), RunState.SUCCEEDED),
        (AttemptOutcome(kind="failed", error={"code": "error"}), RunState.FAILED),
        (AttemptOutcome(kind="cancelled"), RunState.CANCELLED),
        (AttemptOutcome(kind="retry", error={"code": "error"}, delay=5.0), RunState.RETRYING),
        (AttemptOutcome(kind="defer", delay=7.0), RunState.QUEUED),
        (AttemptOutcome(kind="requeue"), RunState.QUEUED),
    ],
)
async def test_transition_kinds(sut, outcome, state):
    run = await claimed(sut)
    now = await sut.store.now()
    after = await sut.store.transition(run.run_id, attempt=1, worker_id=W1, outcome=outcome)
    assert after.state == state
    assert after.worker_id is None and after.lease_expires_at is None
    if state in (RunState.SUCCEEDED, RunState.FAILED, RunState.CANCELLED):
        assert after.finished_at == pytest.approx(now, abs=0.05)
        assert after.expires_at == pytest.approx(after.finished_at + 60.0)
    if outcome.kind == "succeeded":
        assert after.result == {"ok": True}
    if outcome.kind in ("failed", "retry"):
        assert after.failure_count == 1
        assert after.error == {"code": "error"}
    if outcome.kind == "retry":
        assert after.available_at == pytest.approx(now + 5.0, abs=0.05)
    if outcome.kind == "defer":
        assert after.defer_count == 1
        assert after.failure_count == 0
        assert after.available_at == pytest.approx(now + 7.0, abs=0.05)
    if outcome.kind == "requeue":
        assert after.failure_count == 0
    # A second write with the same attempt is fenced out.
    assert (
        await sut.store.transition(
            run.run_id, attempt=1, worker_id=W1, outcome=AttemptOutcome(kind="succeeded")
        )
        is None
    )


async def test_retry_then_claim_bumps_attempt(sut):
    run = await claimed(sut)
    await sut.store.transition(
        run.run_id, attempt=1, worker_id=W1, outcome=AttemptOutcome(kind="retry", delay=0.05)
    )
    await sut.advance(0.06)
    res = await sut.store.claim(run.run_id, worker_id=W2, lease_ttl=TTL)
    assert res.status == ClaimStatus.CLAIMED
    assert res.run.attempt == 2
    assert res.run.error is None
    assert res.run.progress == {}
    assert (
        await sut.store.transition(
            run.run_id, attempt=1, worker_id=W1, outcome=AttemptOutcome(kind="succeeded")
        )
        is None
    )


async def test_cancelling_always_ends_cancelled(sut):
    for outcome in (
        AttemptOutcome(kind="succeeded", result=1),
        AttemptOutcome(kind="retry", delay=1),
    ):
        run = await claimed(sut)
        await sut.store.request_cancel(run.run_id)
        after = await sut.store.transition(run.run_id, attempt=1, worker_id=W1, outcome=outcome)
        assert after.state == RunState.CANCELLED
        assert after.result is None


async def test_terminal_states_are_final(sut):
    run = await claimed(sut)
    await sut.store.transition(
        run.run_id, attempt=1, worker_id=W1, outcome=AttemptOutcome(kind="succeeded", result=1)
    )
    assert (await sut.store.claim(run.run_id, worker_id=W1, lease_ttl=TTL)).status == (
        ClaimStatus.NOT_READY
    )
    res = await sut.store.request_cancel(run.run_id)
    assert res.status == CancelStatus.TERMINAL
    assert res.run.state == RunState.SUCCEEDED
    assert await sut.store.mark_lost(run.run_id, attempt=1) is None


# ---------------------------------------------------------------------- cancel


async def test_request_cancel_states(sut):
    queued = (await sut.store.create_run(new_run())).run
    res = await sut.store.request_cancel(queued.run_id)
    assert res.status == CancelStatus.CANCELLED
    assert res.run.state == RunState.CANCELLED
    assert res.run.finished_at is not None

    retrying = await claimed(sut)
    await sut.store.transition(
        retrying.run_id, attempt=1, worker_id=W1, outcome=AttemptOutcome(kind="retry", delay=100)
    )
    assert (await sut.store.request_cancel(retrying.run_id)).status == CancelStatus.CANCELLED

    running = await claimed(sut)
    assert (await sut.store.request_cancel(running.run_id)).status == CancelStatus.REQUESTED
    assert (await sut.store.request_cancel(running.run_id)).status == (
        CancelStatus.ALREADY_REQUESTED
    )
    assert (await sut.store.request_cancel(uid())).status == CancelStatus.NOT_FOUND


# ---------------------------------------------------------------------- lost leases


async def test_expired_lease_recovery(sut):
    task = uid("task")
    run = await claimed(sut, task=task, max_lost=1)
    assert await sut.store.find_expired_leases() == [] or all(
        lease.run_id != run.run_id for lease in await sut.store.find_expired_leases()
    )
    await sut.advance(TTL + 0.05)
    expired = await sut.store.find_expired_leases()
    assert HeldLease(run_id=run.run_id, attempt=1) in expired
    lost = await sut.store.mark_lost(run.run_id, attempt=1)
    assert lost.run.state == RunState.QUEUED
    assert lost.run.lost_count == 1
    assert lost.run.next_publish_at == pytest.approx(await sut.store.now(), abs=0.05)
    assert lost.run.failure_count == 0
    assert await sut.store.mark_lost(run.run_id, attempt=1) is None

    # The stale worker is fenced out everywhere.
    assert (
        await sut.store.renew_leases(W1, [HeldLease(run_id=run.run_id, attempt=1)], lease_ttl=TTL)
        == {}
    )
    assert not await sut.store.append_events(
        run.run_id, attempt=1, worker_id=W1, events=[EventIn(type="log", at=1.0)]
    )

    second = await sut.store.claim(run.run_id, worker_id=W2, lease_ttl=TTL)
    assert second.run.attempt == 2
    assert (
        await sut.store.transition(
            run.run_id, attempt=1, worker_id=W1, outcome=AttemptOutcome(kind="succeeded")
        )
        is None
    )
    await sut.advance(TTL + 0.05)
    final = await sut.store.mark_lost(run.run_id, attempt=2)
    assert final.run.state == RunState.FAILED
    assert final.run.error["code"] == "lost"
    assert final.run.lost_count == 2


async def test_renewed_lease_is_not_lost(sut):
    run = await claimed(sut)
    await sut.advance(TTL + 0.05)
    # Expired by the clock but renewed before the sweeper got to it: still owned.
    assert await sut.store.renew_leases(
        W1, [HeldLease(run_id=run.run_id, attempt=1)], lease_ttl=TTL
    ) == {run.run_id: False}
    assert await sut.store.mark_lost(run.run_id, attempt=1) is None


async def test_lost_while_cancelling_is_cancelled(sut):
    run = await claimed(sut)
    await sut.store.request_cancel(run.run_id)
    await sut.advance(TTL + 0.05)
    lost = await sut.store.mark_lost(run.run_id, attempt=1)
    assert lost.run.state == RunState.CANCELLED


async def test_lost_releases_slot(sut):
    task = uid("task")
    run = (await sut.store.create_run(new_run(task))).run
    await sut.store.claim(run.run_id, worker_id=W1, lease_ttl=TTL, concurrency_limit=2)
    assert await sut.store.concurrency_count(task) == 1
    await sut.advance(TTL + 0.05)
    await sut.store.mark_lost(run.run_id, attempt=1)
    assert await sut.store.concurrency_count(task) == 0
    await sut.store.recount_concurrency()
    assert await sut.store.concurrency_count(task) == 0


async def test_recount_concurrency(sut):
    task = uid("task")
    for _ in range(2):
        run = (await sut.store.create_run(new_run(task, concurrency_key="k"))).run
        await sut.store.claim(run.run_id, worker_id=W1, lease_ttl=TTL, concurrency_limit=5)
    await sut.store.recount_concurrency()
    assert await sut.store.concurrency_count(task, "k") == 2


# ---------------------------------------------------------------------- publishing and purge


async def test_due_runs_and_mark_published(sut):
    task = uid("task")
    now_run = (await sut.store.create_run(new_run(task))).run
    later = (await sut.store.create_run(new_run(task, delay=0.1))).run
    due = {r.run_id for r in await sut.store.due_runs(limit=1000)}
    assert now_run.run_id in due and later.run_id not in due
    await sut.store.mark_published(
        [now_run, now_run.model_copy(update={"run_id": uid()})], republish_after=0.3
    )
    got = await sut.store.get_run(now_run.run_id)
    assert got.published_at is not None
    assert got.next_publish_at == pytest.approx(got.published_at + 0.3)
    due = {r.run_id for r in await sut.store.due_runs(limit=1000)}
    assert now_run.run_id not in due
    await sut.advance(0.15)
    due = {r.run_id for r in await sut.store.due_runs(limit=1000)}
    assert later.run_id in due
    assert now_run.run_id not in due
    await sut.advance(0.2)
    assert now_run.run_id in {r.run_id for r in await sut.store.due_runs(limit=1000)}


async def test_published_backlog_is_not_republished_at_once(sut):
    """A published but unclaimed run is due again only after its own republish_after."""
    task = uid("task")
    first = (await sut.store.create_run(new_run(task))).run
    await sut.store.mark_published([first], republish_after=0.2)
    await sut.advance(0.1)
    second = (await sut.store.create_run(new_run(task))).run
    await sut.store.mark_published([second], republish_after=0.2)
    await sut.advance(0.15)
    due = {r.run_id for r in await sut.store.due_runs(limit=1000)}
    assert first.run_id in due and second.run_id not in due


async def test_mark_published_ignores_leased_runs(sut):
    created = (await sut.store.create_run(new_run())).run
    claim = await sut.store.claim(created.run_id, worker_id=W1, lease_ttl=TTL)
    run = claim.run
    await sut.store.mark_published([created])
    got = await sut.store.get_run(run.run_id)
    assert got.next_publish_at is None
    assert run.run_id not in {r.run_id for r in await sut.store.due_runs(limit=1000)}


async def test_stale_mark_published_keeps_a_newer_retry_time(sut):
    """H2: publish, a fast claim and a failure set a 1 s retry; the publisher's mark that
    arrives after must not push the retry to now + republish_after."""
    created = (await sut.store.create_run(new_run())).run
    claim = await sut.store.claim(created.run_id, worker_id=W1, lease_ttl=TTL)
    assert claim.status == ClaimStatus.CLAIMED
    retrying = await sut.store.transition(
        created.run_id, attempt=1, worker_id=W1, outcome=AttemptOutcome(kind="retry", delay=1.0)
    )
    await sut.store.mark_published([created], republish_after=300.0)
    got = await sut.store.get_run(created.run_id)
    assert got.state == RunState.RETRYING
    assert got.next_publish_at == got.available_at == retrying.available_at
    assert got.published_at is None
    # The current value still matches: that mark applies.
    await sut.store.mark_published([got], republish_after=300.0)
    marked = await sut.store.get_run(created.run_id)
    assert marked.published_at is not None
    assert marked.next_publish_at == pytest.approx(marked.published_at + 300.0)


async def test_mark_published_cas_on_defer_and_requeue(sut):
    for outcome in (AttemptOutcome(kind="defer", delay=0.5), AttemptOutcome(kind="requeue")):
        created = (await sut.store.create_run(new_run())).run
        await sut.store.claim(created.run_id, worker_id=W1, lease_ttl=TTL)
        after = await sut.store.transition(created.run_id, attempt=1, worker_id=W1, outcome=outcome)
        await sut.store.mark_published([created], republish_after=300.0)
        got = await sut.store.get_run(created.run_id)
        assert got.next_publish_at == after.next_publish_at == got.available_at


async def test_mark_published_rejects_a_plain_id_list(sut):
    run = (await sut.store.create_run(new_run())).run
    with pytest.raises(TypeError):
        await sut.store.mark_published([run.run_id])  # type: ignore[list-item]
    await sut.store.mark_published([run.model_copy(update={"next_publish_at": None})])
    assert (await sut.store.get_run(run.run_id)).published_at is None


# ---------------------------------------------------------------------- storable values

NOT_STORABLE = {
    "nul": "a\x00b",
    "nul_key": {"k\x00": 1},
    "nul_nested": {"a": [{"b": "x\x00"}]},
    "nan": float("nan"),
    "inf": float("inf"),
    "nested_nan": {"a": [1.0, float("-inf")]},
}


@pytest.mark.parametrize("label", sorted(NOT_STORABLE))
async def test_result_not_storable_raises_and_writes_nothing(sut, label):
    """H1: every store refuses the same values before writing; the run stays leased."""
    run = await claimed(sut)
    with pytest.raises(ValueNotStorable) as info:
        await sut.store.transition(
            run.run_id,
            attempt=1,
            worker_id=W1,
            outcome=AttemptOutcome(kind="succeeded", result={"v": NOT_STORABLE[label]}),
        )
    assert info.value.field == "result"
    got = await sut.store.get_run(run.run_id)
    assert got.state == RunState.RUNNING and got.worker_id == W1 and got.result is None
    done = await sut.store.transition(
        run.run_id,
        attempt=1,
        worker_id=W1,
        outcome=AttemptOutcome(kind="succeeded", result={"v": "a\\u0000 is text"}),
    )
    assert done.state == RunState.SUCCEEDED and done.result == {"v": "a\\u0000 is text"}


@pytest.mark.parametrize("label", ["nul", "nan", "nul_key"])
async def test_event_data_not_storable(sut, label):
    run = await claimed(sut)
    bad = EventIn(type="log", at=1.0, data={"v": NOT_STORABLE[label]})
    with pytest.raises(ValueNotStorable) as info:
        await sut.store.append_events(
            run.run_id, attempt=1, worker_id=W1, events=[EventIn(type="ok", at=1.0), bad]
        )
    assert info.value.field == "event.data"
    assert await sut.store.read_events(run.run_id) == []
    with pytest.raises(ValueNotStorable) as info:
        await sut.store.append_events(
            run.run_id, attempt=1, worker_id=W1, events=[], progress={"p": NOT_STORABLE[label]}
        )
    assert info.value.field == "progress"
    with pytest.raises(ValueNotStorable) as info:
        await sut.store.append_events(
            run.run_id, attempt=1, worker_id=W1, events=[EventIn(type="t\x00", at=1.0)]
        )
    assert info.value.field == "event.type"
    assert (await sut.store.get_run(run.run_id)).progress == {}


@pytest.mark.parametrize("label", ["nul", "nan", "nul_key"])
async def test_kwargs_not_storable(sut, label):
    new = new_run(kwargs={"v": NOT_STORABLE[label]})
    with pytest.raises(ValueNotStorable) as info:
        await sut.store.create_run(new)
    assert info.value.field == "kwargs"
    assert await sut.store.get_run(new.run_id) is None


async def test_tenant_none_is_one_value_for_keys(sut):
    task = uid("task")
    first = await sut.store.create_run(new_run(task, idempotency_key="k", tenant=None))
    again = await sut.store.create_run(new_run(task, idempotency_key="k", tenant=None))
    assert first.created and not again.created
    assert again.run.run_id == first.run.run_id
    assert first.run.tenant == ""
    other = await sut.store.create_run(new_run(task, idempotency_key="k", tenant="org-1"))
    assert other.created


async def test_retrying_run_becomes_due(sut):
    run = await claimed(sut)
    await sut.store.mark_published([run])
    await sut.store.transition(
        run.run_id, attempt=1, worker_id=W1, outcome=AttemptOutcome(kind="retry", delay=0.1)
    )
    assert run.run_id not in {r.run_id for r in await sut.store.due_runs(limit=1000)}
    await sut.advance(0.15)
    assert run.run_id in {r.run_id for r in await sut.store.due_runs(limit=1000)}


async def test_purge_expired_frees_key(sut):
    task = uid("task")
    run = await claimed(sut, task=task, idempotency_key="k", retention=0.0)
    await sut.store.append_events(
        run.run_id, attempt=1, worker_id=W1, events=[EventIn(type="log", at=1)]
    )
    await sut.store.transition(
        run.run_id, attempt=1, worker_id=W1, outcome=AttemptOutcome(kind="succeeded")
    )
    keep = (await sut.store.create_run(new_run(retention=0.0))).run
    await sut.advance(0.01)
    assert await sut.store.purge_expired() >= 1
    assert await sut.store.get_run(run.run_id) is None
    assert await sut.store.read_events(run.run_id) == []
    assert await sut.store.get_run(keep.run_id) is not None
    again = await sut.store.create_run(new_run(task, idempotency_key="k"))
    assert again.created


async def test_no_retention_never_expires(sut):
    run = await claimed(sut, retention=None)
    after = await sut.store.transition(
        run.run_id, attempt=1, worker_id=W1, outcome=AttemptOutcome(kind="succeeded")
    )
    assert after.expires_at is None


async def test_clock_moves_forward(sut):
    first = await sut.store.now()
    await sut.advance(0.01)
    assert await sut.store.now() >= first


# ---------------------------------------------------------------------- races (real concurrency)


async def test_concurrent_claims_one_winner(sut):
    run = (await sut.store.create_run(new_run())).run
    results = await asyncio.gather(
        *(sut.store.claim(run.run_id, worker_id=f"w{i}", lease_ttl=TTL) for i in range(20))
    )
    winners = [r for r in results if r.status == ClaimStatus.CLAIMED]
    assert len(winners) == 1
    assert (await sut.store.get_run(run.run_id)).attempt == 1


async def test_concurrent_claims_respect_cap(sut):
    task = uid("task")
    runs = [(await sut.store.create_run(new_run(task, concurrency_key="k"))).run for _ in range(12)]
    results = await asyncio.gather(
        *(
            sut.store.claim(r.run_id, worker_id=f"w{i}", lease_ttl=TTL, concurrency_limit=3)
            for i, r in enumerate(runs)
        ),
        sut.store.recount_concurrency(),
    )
    claimed_ = [r for r in results[:-1] if r.status == ClaimStatus.CLAIMED]
    assert len(claimed_) == 3
    assert await sut.store.concurrency_count(task, "k") == 3
    await sut.store.recount_concurrency()
    assert await sut.store.concurrency_count(task, "k") == 3


async def test_concurrent_finish_and_claim_keep_counter_exact(sut):
    task = uid("task")
    runs = [(await sut.store.create_run(new_run(task))).run for _ in range(6)]
    first = [
        await sut.store.claim(r.run_id, worker_id=W1, lease_ttl=TTL, concurrency_limit=3)
        for r in runs[:3]
    ]
    assert all(c.status == ClaimStatus.CLAIMED for c in first)
    await asyncio.gather(
        *(
            sut.store.transition(
                c.run.run_id, attempt=1, worker_id=W1, outcome=AttemptOutcome(kind="succeeded")
            )
            for c in first
        ),
        *(
            sut.store.claim(r.run_id, worker_id=W2, lease_ttl=TTL, concurrency_limit=3)
            for r in runs[3:]
        ),
        sut.store.recount_concurrency(),
    )
    leased = [
        r
        for r in (await sut.store.list_runs(RunFilter(task=task), limit=100)).items
        if r.state == RunState.RUNNING
    ]
    assert await sut.store.concurrency_count(task) == len(leased) <= 3


async def test_replace_races_claim(sut):
    task = uid("task")
    run = (await sut.store.create_run(new_run(task, idempotency_key="k"))).run
    replaced, claim = await asyncio.gather(
        sut.store.create_run(
            new_run(task, idempotency_key="k", kwargs={"x": 9}), key_mode="replace"
        ),
        sut.store.claim(run.run_id, worker_id=W1, lease_ttl=TTL),
    )
    assert claim.status == ClaimStatus.CLAIMED
    stored = await sut.store.get_run(run.run_id)
    # Either the replace landed first (the claimed run has the new kwargs) or it saw the run
    # running and changed nothing: never a running run with half-applied changes.
    if replaced.replaced:
        assert stored.kwargs == {"x": 9}
        assert claim.run.kwargs == {"x": 9}
    else:
        assert stored.kwargs == {"x": 1}


async def test_renew_races_mark_lost(sut):
    run = await claimed(sut)
    await sut.advance(TTL + 0.05)
    renewed, lost = await asyncio.gather(
        sut.store.renew_leases(W1, [HeldLease(run_id=run.run_id, attempt=1)], lease_ttl=TTL),
        sut.store.mark_lost(run.run_id, attempt=1),
    )
    stored = await sut.store.get_run(run.run_id)
    # Exactly one side wins.
    assert (renewed == {run.run_id: False}) != (lost is not None)
    if lost is not None:
        assert stored.state == RunState.QUEUED and stored.worker_id is None
    else:
        assert stored.state == RunState.RUNNING and stored.worker_id == W1


# ---------------------------------------------------------------------- optional claim_next


async def test_claim_next(sut):
    queue = uid("q")
    task = uid("task")
    low = (await sut.store.create_run(new_run(task, queue=queue, priority="low"))).run
    await sut.advance(0.01)
    high = (await sut.store.create_run(new_run(task, queue=queue, priority="high"))).run
    capped_task = uid("task")
    blocker = (await sut.store.create_run(new_run(capped_task, queue=queue, priority="high"))).run
    await sut.store.claim(blocker.run_id, worker_id=W2, lease_ttl=10, concurrency_limit=1)
    waiting = (await sut.store.create_run(new_run(capped_task, queue=queue, priority="high"))).run
    try:
        first = await sut.store.claim_next(
            [queue], worker_id=W1, lease_ttl=TTL, concurrency_limits={capped_task: 1}
        )
    except NotImplementedError:
        pytest.skip("store has no claim_next")
    assert first.run_id == high.run_id
    assert first.attempt == 1 and first.worker_id == W1
    second = await sut.store.claim_next(
        [queue], worker_id=W1, lease_ttl=TTL, concurrency_limits={capped_task: 1}
    )
    assert second.run_id == low.run_id  # the capped run is skipped, not taken
    assert (await sut.store.get_run(waiting.run_id)).state == RunState.QUEUED
    assert (
        await sut.store.claim_next(
            [queue], worker_id=W1, lease_ttl=TTL, concurrency_limits={capped_task: 1}
        )
        is None
    )
    assert await sut.store.claim_next([uid("q")], worker_id=W1, lease_ttl=TTL) is None
