"""DB-backed job queue.

State machine::

    queued --claim_next--> running --complete--> succeeded
                              |
                              +--fail--> queued (attempts < max_attempts, after backoff)
                              +--fail--> failed (attempts exhausted)
    queued --cancel--> cancelled

Every transition is a conditional ``UPDATE ... WHERE id = :id AND status =
:expected`` so that concurrent workers never claim or finish the same job
twice; only portable SQL is used (no ``SKIP LOCKED`` / ``RETURNING``).
``attempts`` is incremented when a job is claimed. Running jobs whose
``started_at`` is older than the timeout are failed (and retried while
attempts remain) by ``reclaim_stale``.
"""

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.clock import Clock
from app.models import Job

JOB_TYPES = ("rca", "repair")
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled")
ACTIVE_STATUSES = ("queued", "running")

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 30
MAX_BACKOFF_SECONDS = 3600
MAX_ERROR_CHARS = 2000
CLAIM_CANDIDATES = 10
TIMEOUT_ERROR = "job timed out"


def backoff_delay(attempts: int, base_seconds: float) -> timedelta:
    """Exponential backoff: base, 2*base, 4*base... capped at one hour."""
    exponent = max(attempts - 1, 0)
    return timedelta(seconds=min(base_seconds * (2**exponent), MAX_BACKOFF_SECONDS))


def _transition(db: Session, job_id: int, expected: str, **values) -> bool:
    result = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == expected)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    return result.rowcount == 1


def _reload(db: Session, job_id: int) -> Job:
    return db.get(Job, job_id, populate_existing=True)


def enqueue(
    db: Session,
    job_type: str,
    clock: Clock,
    *,
    incident_id: int | None = None,
    application_id: int | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    run_after: datetime | None = None,
) -> Job:
    if job_type not in JOB_TYPES:
        raise ValueError(f"unknown job type: {job_type}")
    now = clock.now()
    job = Job(
        type=job_type,
        incident_id=incident_id,
        application_id=application_id,
        status="queued",
        attempts=0,
        max_attempts=max(max_attempts, 1),
        run_after=run_after or now,
        created_at=now,
    )
    db.add(job)
    db.commit()
    return job


def active_job(db: Session, job_type: str, incident_id: int) -> Job | None:
    """The queued or running job of ``job_type`` for the incident, if any."""
    return db.scalar(
        select(Job)
        .where(
            Job.type == job_type,
            Job.incident_id == incident_id,
            Job.status.in_(ACTIVE_STATUSES),
        )
        .order_by(Job.id)
        .limit(1)
    )


def claim_next(db: Session, clock: Clock, types: tuple[str, ...] | None = None) -> Job | None:
    """Atomically move the oldest due queued job to ``running`` and return it."""
    now = clock.now()
    conditions = [Job.status == "queued", Job.run_after <= now]
    if types:
        conditions.append(Job.type.in_(types))
    candidates = db.scalars(
        select(Job.id)
        .where(*conditions)
        .order_by(Job.run_after, Job.id)
        .limit(CLAIM_CANDIDATES)
    ).all()
    for job_id in candidates:
        claimed = _transition(
            db,
            job_id,
            "queued",
            status="running",
            started_at=now,
            finished_at=None,
            attempts=Job.attempts + 1,
        )
        if claimed:
            return _reload(db, job_id)
    return None


def complete(db: Session, job: Job, clock: Clock, result_ref: str | None = None) -> bool:
    done = _transition(
        db,
        job.id,
        "running",
        status="succeeded",
        finished_at=clock.now(),
        result_ref=result_ref,
        error=None,
    )
    _reload(db, job.id)
    return done


def fail(
    db: Session,
    job: Job,
    error: str,
    clock: Clock,
    *,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
) -> bool:
    """Record a failed run; requeue with backoff while attempts remain."""
    current = _reload(db, job.id)
    now = clock.now()
    error = (error or "unknown error")[:MAX_ERROR_CHARS]
    if current.attempts < current.max_attempts:
        values = {
            "status": "queued",
            "run_after": now + backoff_delay(current.attempts, backoff_seconds),
            "error": error,
        }
    else:
        values = {"status": "failed", "finished_at": now, "error": error}
    done = _transition(db, job.id, "running", **values)
    _reload(db, job.id)
    return done


def cancel(db: Session, job: Job, clock: Clock) -> bool:
    done = _transition(db, job.id, "queued", status="cancelled", finished_at=clock.now())
    _reload(db, job.id)
    return done


def reclaim_stale(
    db: Session,
    clock: Clock,
    timeout_seconds: float,
    *,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
) -> list[Job]:
    """Fail running jobs older than the timeout (requeued if attempts remain)."""
    cutoff = clock.now() - timedelta(seconds=timeout_seconds)
    stale = db.scalars(
        select(Job).where(Job.status == "running", Job.started_at < cutoff).order_by(Job.id)
    ).all()
    reclaimed: list[Job] = []
    for job in stale:
        message = f"{TIMEOUT_ERROR} after {int(timeout_seconds)}s"
        if fail(db, job, message, clock, backoff_seconds=backoff_seconds):
            reclaimed.append(_reload(db, job.id))
    return reclaimed
