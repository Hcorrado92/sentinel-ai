"""Job handlers, registered by job type.

A handler receives a ``JobContext`` and returns an optional ``result_ref``
(e.g. ``"rca_report:12"``). Raising marks the run as failed; the queue retries
it while attempts remain.

``rca`` runs the RCA engine; ``repair`` runs the repair engine on the latest
completed RCA and, when the repair is verified by tests, confirms the matching
hypothesis. A repair that does not succeed fails the job with its stop reason.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.base import AIProvider
from app.ai.registry import get_provider
from app.clock import Clock
from app.config import Settings
from app.models import AuthorizedRepository, Event, Incident, Job, RCAReport
from app.rca.engine import run_rca
from app.repair.confirm import confirm_from_repair
from app.repair.engine import run_repair
from app.repo_tools import RepoAccessError, RepoReader
from app.runner import AllowListRunner, CommandRunner, SubprocessRunner

MAX_RCA_EVENTS = 20
GIT_TIMEOUT_SECONDS = 120.0


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


def repair_repository_for(db: Session, application_id: int) -> AuthorizedRepository | None:
    """The application's first authorized repository usable for repairs."""
    repositories = db.scalars(
        select(AuthorizedRepository)
        .where(AuthorizedRepository.application_id == application_id)
        .order_by(AuthorizedRepository.id)
    ).all()
    for repository in repositories:
        if (repository.local_path or repository.clone_url) and repository.test_command:
            return repository
    return None


def latest_completed_rca(db: Session, incident_id: int) -> RCAReport | None:
    return db.scalar(
        select(RCAReport)
        .where(RCAReport.incident_id == incident_id, RCAReport.status == "completed")
        .order_by(RCAReport.created_at.desc(), RCAReport.id.desc())
        .limit(1)
    )


def make_repair_handler(
    provider_factory: ProviderFactory,
    settings: Settings,
    git_runner: CommandRunner,
    test_runner: CommandRunner,
) -> Handler:
    def handle_repair(ctx: JobContext) -> str | None:
        db = ctx.db
        incident = db.get(Incident, ctx.job.incident_id) if ctx.job.incident_id else None
        if incident is None:
            raise JobError(f"incident {ctx.job.incident_id} not found")
        rca = latest_completed_rca(db, incident.id)
        if rca is None:
            raise JobError("no completed RCA report for this incident")
        repository = repair_repository_for(db, incident.application_id)
        if repository is None:
            raise JobError("the application has no authorized repository")

        result = run_repair(
            db,
            incident,
            rca.report,
            repository,
            provider_factory(),
            git_runner,
            ctx.clock,
            settings,
            test_runner=test_runner,
            job_id=ctx.job.id,
        )
        last = result.attempts[-1] if result.attempts else None
        if result.succeeded and last is not None:
            confirm_from_repair(db, rca, last, ctx.clock.now())
            db.commit()
            return f"repair_attempt:{last.id}"
        # The engine already retried internally; the outcome is final.
        raise JobError(f"repair {result.status}: {result.stop_reason or 'unknown'}")

    return handle_repair


def build_handlers(
    settings: Settings,
    provider_factory: ProviderFactory | None = None,
    *,
    git_runner: CommandRunner | None = None,
    test_runner: CommandRunner | None = None,
) -> dict[str, Handler]:
    """Default handler registry. The provider is built lazily, per job.

    ``git_runner`` always goes through the git/gh allow-list; ``test_runner``
    runs the repository's fixed test command.
    """
    factory = provider_factory or (lambda: get_provider(settings))
    git = AllowListRunner(git_runner or SubprocessRunner(default_timeout=GIT_TIMEOUT_SECONDS))
    tests = test_runner or SubprocessRunner(default_timeout=settings.repair_test_timeout_seconds)
    return {
        "rca": make_rca_handler(factory),
        "repair": make_repair_handler(factory, settings, git, tests),
    }
