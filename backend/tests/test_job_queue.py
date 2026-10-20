from datetime import datetime, timedelta, timezone

import pytest

from app.clock import FakeClock
from app.db import create_db_engine, create_session_factory, init_db
from app.jobs import queue
from app.models import Job
from app.worker import run_once

START = datetime(2024, 3, 1, 10, tzinfo=timezone.utc)


@pytest.fixture
def session_factory(database_url):
    engine = create_db_engine(database_url)
    init_db(engine)
    yield create_session_factory(engine)
    engine.dispose()


@pytest.fixture
def db(session_factory):
    session = session_factory()
    yield session
    session.close()


@pytest.fixture
def clock():
    return FakeClock(START)


def _job(session_factory, job_id) -> Job:
    with session_factory() as session:
        return session.get(Job, job_id)


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# --- queue primitives -----------------------------------------------------------


def test_enqueue_creates_queued_job(db, clock):
    job = queue.enqueue(db, "rca", clock, max_attempts=4)

    stored = db.get(Job, job.id)
    assert stored.type == "rca"
    assert stored.status == "queued"
    assert stored.attempts == 0
    assert stored.max_attempts == 4
    assert _utc(stored.run_after) == START
    assert stored.started_at is None and stored.finished_at is None


def test_enqueue_rejects_unknown_type(db, clock):
    with pytest.raises(ValueError):
        queue.enqueue(db, "deploy", clock)


def test_claim_next_marks_running_and_counts_attempt(db, clock):
    first = queue.enqueue(db, "rca", clock)
    second = queue.enqueue(db, "repair", clock)

    claimed = queue.claim_next(db, clock)
    assert claimed.id == first.id
    assert claimed.status == "running"
    assert claimed.attempts == 1
    assert _utc(claimed.started_at) == START

    assert queue.claim_next(db, clock).id == second.id
    assert queue.claim_next(db, clock) is None


def test_claim_next_filters_by_type_and_respects_run_after(db, clock):
    later = queue.enqueue(db, "rca", clock, run_after=START + timedelta(minutes=5))
    repair = queue.enqueue(db, "repair", clock)

    assert queue.claim_next(db, clock, ("rca",)) is None
    clock.advance(minutes=5)
    assert queue.claim_next(db, clock, ("rca",)).id == later.id
    assert db.get(Job, repair.id, populate_existing=True).status == "queued"


def test_claim_is_atomic_across_sessions(session_factory, clock):
    with session_factory() as setup:
        job = queue.enqueue(setup, "rca", clock)

    with session_factory() as worker_a, session_factory() as worker_b:
        assert queue.claim_next(worker_a, clock).id == job.id
        assert queue.claim_next(worker_b, clock) is None
        # A conditional transition from a stale state never applies twice.
        assert queue._transition(worker_b, job.id, "queued", status="running") is False

    assert _job(session_factory, job.id).attempts == 1


def test_complete_records_result(db, clock):
    job = queue.enqueue(db, "rca", clock)
    queue.claim_next(db, clock)
    clock.advance(seconds=2)

    assert queue.complete(db, job, clock, "rca_report:7") is True
    stored = db.get(Job, job.id)
    assert stored.status == "succeeded"
    assert stored.result_ref == "rca_report:7"
    assert _utc(stored.finished_at) == START + timedelta(seconds=2)
    # Completing twice is a no-op.
    assert queue.complete(db, job, clock, "rca_report:8") is False


def test_fail_requeues_with_exponential_backoff_then_fails(db, clock):
    job = queue.enqueue(db, "rca", clock, max_attempts=2)
    queue.claim_next(db, clock)

    queue.fail(db, job, "boom", clock, backoff_seconds=10)
    stored = db.get(Job, job.id)
    assert stored.status == "queued"
    assert stored.error == "boom"
    assert _utc(stored.run_after) == START + timedelta(seconds=10)

    clock.advance(seconds=10)
    queue.claim_next(db, clock)
    queue.fail(db, job, "boom again", clock, backoff_seconds=10)
    stored = db.get(Job, job.id)
    assert stored.status == "failed"
    assert stored.attempts == 2
    assert stored.error == "boom again"
    assert stored.finished_at is not None


def test_backoff_delay_doubles_and_is_capped():
    assert queue.backoff_delay(1, 30) == timedelta(seconds=30)
    assert queue.backoff_delay(2, 30) == timedelta(seconds=60)
    assert queue.backoff_delay(3, 30) == timedelta(seconds=120)
    assert queue.backoff_delay(20, 30) == timedelta(seconds=queue.MAX_BACKOFF_SECONDS)


def test_cancel_only_applies_to_queued_jobs(db, clock):
    queued = queue.enqueue(db, "rca", clock)
    assert queue.cancel(db, queued, clock) is True
    assert db.get(Job, queued.id).status == "cancelled"
    assert queue.claim_next(db, clock) is None

    running = queue.enqueue(db, "rca", clock)
    queue.claim_next(db, clock)
    assert queue.cancel(db, running, clock) is False
    assert db.get(Job, running.id).status == "running"


# --- timeouts -------------------------------------------------------------------


def test_stale_running_job_is_requeued_while_attempts_remain(db, clock):
    job = queue.enqueue(db, "rca", clock, max_attempts=2)
    queue.claim_next(db, clock)

    clock.advance(seconds=59)
    assert queue.reclaim_stale(db, clock, 60) == []
    clock.advance(seconds=2)
    reclaimed = queue.reclaim_stale(db, clock, 60, backoff_seconds=5)

    assert [j.id for j in reclaimed] == [job.id]
    stored = db.get(Job, job.id)
    assert stored.status == "queued"
    assert stored.attempts == 1
    assert queue.TIMEOUT_ERROR in stored.error


def test_stale_running_job_without_attempts_left_is_failed(db, clock):
    job = queue.enqueue(db, "rca", clock, max_attempts=1)
    queue.claim_next(db, clock)

    clock.advance(seconds=120)
    queue.reclaim_stale(db, clock, 60)

    stored = db.get(Job, job.id)
    assert stored.status == "failed"
    assert queue.TIMEOUT_ERROR in stored.error
    assert stored.finished_at is not None


def test_run_once_reclaims_stale_job_and_reruns_it(session_factory, clock):
    with session_factory() as db:
        job = queue.enqueue(db, "rca", clock, max_attempts=3)
        queue.claim_next(db, clock)  # a worker claims it and dies
    clock.advance(seconds=700)

    processed = run_once(
        session_factory,
        {"rca": lambda ctx: "rca_report:1"},
        clock=clock,
        job_timeout_seconds=600,
        backoff_seconds=0,
    )

    assert processed.id == job.id
    assert processed.status == "succeeded"
    assert processed.attempts == 2


def test_run_once_fails_stale_job_with_exhausted_attempts(session_factory, clock):
    with session_factory() as db:
        job = queue.enqueue(db, "rca", clock, max_attempts=1)
        queue.claim_next(db, clock)
    clock.advance(seconds=700)

    assert run_once(session_factory, {"rca": lambda ctx: None}, clock=clock, job_timeout_seconds=600) is None
    stored = _job(session_factory, job.id)
    assert stored.status == "failed"
    assert queue.TIMEOUT_ERROR in stored.error


# --- worker -------------------------------------------------------------------


def test_run_once_returns_none_when_idle(session_factory, clock):
    assert run_once(session_factory, {"rca": lambda ctx: None}, clock=clock) is None


def test_run_once_dispatches_by_type(session_factory, clock):
    calls = []
    with session_factory() as db:
        rca = queue.enqueue(db, "rca", clock)
        repair = queue.enqueue(db, "repair", clock)

    handlers = {
        "rca": lambda ctx: calls.append(("rca", ctx.job.id)) or "rca_report:1",
        "repair": lambda ctx: calls.append(("repair", ctx.job.id)) or "repair_attempt:1",
    }
    assert run_once(session_factory, handlers, clock=clock).result_ref == "rca_report:1"
    assert run_once(session_factory, handlers, clock=clock).result_ref == "repair_attempt:1"
    assert calls == [("rca", rca.id), ("repair", repair.id)]


def test_jobs_without_registered_handler_are_left_queued(session_factory, clock):
    with session_factory() as db:
        job = queue.enqueue(db, "repair", clock)

    assert run_once(session_factory, {"rca": lambda ctx: None}, clock=clock) is None
    assert _job(session_factory, job.id).status == "queued"


def test_raising_handler_is_retried_until_max_attempts_then_failed(session_factory, clock):
    calls = []

    def broken(ctx):
        calls.append(ctx.job.attempts)
        raise RuntimeError("handler exploded token=fictitious-secret-value")

    with session_factory() as db:
        job = queue.enqueue(db, "rca", clock, max_attempts=3)

    handlers = {"rca": broken}
    first = run_once(session_factory, handlers, clock=clock, backoff_seconds=10)
    assert first.status == "queued"
    assert first.attempts == 1
    assert "handler exploded" in first.error

    # Not due yet: backoff applies.
    assert run_once(session_factory, handlers, clock=clock, backoff_seconds=10) is None

    clock.advance(seconds=10)
    second = run_once(session_factory, handlers, clock=clock, backoff_seconds=10)
    assert second.status == "queued"
    assert second.attempts == 2

    clock.advance(seconds=20)
    last = run_once(session_factory, handlers, clock=clock, backoff_seconds=10)
    assert last.id == job.id
    assert last.status == "failed"
    assert last.attempts == 3
    assert "RuntimeError: handler exploded" in last.error
    assert "fictitious-secret-value" not in last.error
    assert last.finished_at is not None
    assert calls == [1, 2, 3]

    clock.advance(hours=2)
    assert run_once(session_factory, handlers, clock=clock, backoff_seconds=10) is None
    assert calls == [1, 2, 3]
