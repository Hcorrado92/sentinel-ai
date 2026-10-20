"""Background worker: ``python -m app.worker``.

``run_once`` reclaims timed-out jobs, claims the next due job, dispatches it
to the handler registered for its type and records the outcome; a run that
takes longer than the job timeout is recorded as failed ("job timed out")
even if its handler returned. The loop
entry point polls the database every ``SENTINEL_WORKER_POLL_SECONDS``.
"""

import logging
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock, SystemClock
from app.config import Settings, get_settings
from app.db import create_db_engine, create_session_factory, init_db
from app.jobs import queue
from app.jobs.handlers import Handlers, JobContext, build_handlers
from app.models import Job
from app.security.redaction import redact_text

logger = logging.getLogger("sentinel.worker")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _error_reason(exc: BaseException) -> str:
    return redact_text(f"{type(exc).__name__}: {exc}")[: queue.MAX_ERROR_CHARS]


def run_once(
    session_factory: sessionmaker[Session],
    handlers: Handlers,
    *,
    clock: Clock | None = None,
    job_timeout_seconds: float = 600,
    backoff_seconds: float = queue.DEFAULT_BACKOFF_SECONDS,
) -> Job | None:
    """Process at most one job; return it (in its final state) or ``None``."""
    clock = clock or SystemClock()
    with session_factory() as db:
        queue.reclaim_stale(db, clock, job_timeout_seconds, backoff_seconds=backoff_seconds)
        job = queue.claim_next(db, clock, tuple(handlers) or None)
        if job is None:
            return None

        handler = handlers[job.type]
        started = clock.now()
        try:
            result_ref = handler(JobContext(db=db, job=job, clock=clock))
        except Exception as exc:  # noqa: BLE001 - every failure is recorded on the job
            db.rollback()
            logger.warning("job %s (%s) failed: %s", job.id, job.type, type(exc).__name__)
            queue.fail(db, job, _error_reason(exc), clock, backoff_seconds=backoff_seconds)
        else:
            elapsed = (_as_utc(clock.now()) - _as_utc(started)).total_seconds()
            if elapsed > job_timeout_seconds:
                # A run that overran the job timeout never counts as a success.
                logger.warning("job %s (%s) timed out", job.id, job.type)
                message = f"{queue.TIMEOUT_ERROR} after {int(job_timeout_seconds)}s"
                queue.fail(db, job, message, clock, backoff_seconds=backoff_seconds)
            else:
                queue.complete(db, job, clock, result_ref)
        return db.get(Job, job.id, populate_existing=True)


def run_forever(settings: Settings, handlers: Handlers | None = None) -> None:
    engine = create_db_engine(settings.database_url)
    init_db(engine)
    session_factory = create_session_factory(engine)
    handlers = handlers or build_handlers(settings)
    logger.info("worker started (job types: %s)", ", ".join(handlers))
    try:
        while True:
            job = run_once(
                session_factory,
                handlers,
                job_timeout_seconds=settings.job_timeout_seconds,
                backoff_seconds=settings.job_retry_backoff_seconds,
            )
            if job is None:
                time.sleep(settings.worker_poll_seconds)
    except KeyboardInterrupt:
        logger.info("worker stopped")
    finally:
        engine.dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    run_forever(get_settings())


if __name__ == "__main__":
    main()
