"""Job handlers, registered by job type.

A handler receives a ``JobContext`` and returns an optional ``result_ref``
(e.g. ``"rca_report:12"``). Raising marks the run as failed; the queue retries
it while attempts remain.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.base import AIProvider
from app.ai.registry import get_provider
from app.clock import Clock
from app.config import Settings
from app.models import AuthorizedRepository, Event, Incident, Job
from app.rca.engine import run_rca
from app.repo_tools import RepoAccessError, RepoReader

MAX_RCA_EVENTS = 20


@dataclass
class JobContext:
    db: Session
    job: Job
    clock: Clock


Handler = Callable[[JobContext], str | None]
Handlers = Mapping[str, Handler]
ProviderFactory = Callable[[], AIProvider]


class JobError(Exception):
    """A handler could not complete its job."""


def repo_reader_for(db: Session, application_id: int) -> RepoReader | None:
    """Read-only reader over the application's first authorized local repository."""
    repositories = db.scalars(
        select(AuthorizedRepository)
        .where(
            AuthorizedRepository.application_id == application_id,
            AuthorizedRepository.local_path.is_not(None),
        )
        .order_by(AuthorizedRepository.id)
    ).all()
    for repository in repositories:
        try:
            return RepoReader(repository.local_path)
        except RepoAccessError:
            continue
    return None


def make_rca_handler(provider_factory: ProviderFactory) -> Handler:
    def handle_rca(ctx: JobContext) -> str | None:
        db = ctx.db
        incident = db.get(Incident, ctx.job.incident_id) if ctx.job.incident_id else None
        if incident is None:
            raise JobError(f"incident {ctx.job.incident_id} not found")

        previous_status = incident.status
        incident.status = "investigating"
        db.commit()
        try:
            events = db.scalars(
                select(Event)
                .where(Event.incident_id == incident.id)
                .order_by(Event.timestamp.desc(), Event.id.desc())
                .limit(MAX_RCA_EVENTS)
            ).all()
            reader = repo_reader_for(db, incident.application_id)
            report = run_rca(db, incident, events, reader, provider_factory(), ctx.clock)
            report.job_id = ctx.job.id
            db.commit()
            if report.status != "completed":
                raise JobError(report.error or "RCA failed")
        except Exception:
            db.rollback()
            incident = db.get(Incident, incident.id, populate_existing=True)
            incident.status = previous_status
            db.commit()
            raise
        return f"rca_report:{report.id}"

    return handle_rca


def build_handlers(settings: Settings, provider_factory: ProviderFactory | None = None) -> dict[str, Handler]:
    """Default handler registry. The provider is built lazily, per job."""
    factory = provider_factory or (lambda: get_provider(settings))
    return {"rca": make_rca_handler(factory)}
