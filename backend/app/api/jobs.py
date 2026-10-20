"""RCA and repair job endpoints and the run history.

``POST /incidents/{id}/rca`` enqueues an RCA job (202, or 409 when one is
already queued/running). ``POST /incidents/{id}/repair`` enqueues a repair job
(409 without a completed RCA or while a repair job is active, 422 when the
application has no authorized repository); ``GET /incidents/{id}/repairs``
lists the repair attempts. ``GET /runs`` lists RCA and repair runs with their
result and cost (``null`` when the provider reports none, e.g. the mock).
With ``SENTINEL_RUN_JOBS_INLINE=true`` the job is processed before responding.
All endpoints require the admin bearer token.
"""

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dashboard import MAX_PAGE_SIZE, _get_incident
from app.clock import Clock, get_clock
from app.config import Settings
from app.db import get_db
from app.jobs import queue
from app.jobs.handlers import latest_completed_rca, repair_repository_for
from app.models import AuditLog, Incident, Job, RCAReport, RepairAttempt
from app.security.auth import require_admin
from app.security.redaction import redact_obj, redact_text

JobType = Literal["rca", "repair"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class JobAccepted(BaseModel):
    job_id: int
    status: str


class RCAReportOut(BaseModel):
    id: int
    incident_id: int
    job_id: int | None
    status: str
    summary: str
    report: dict[str, Any]
    provider: str | None
    model: str | None
    is_mock: bool
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    error: str | None
    duration_ms: int | None
    created_at: datetime
    finished_at: datetime | None


class RunOut(BaseModel):
    id: int
    type: str
    status: str
    incident_id: int | None
    incident_title: str | None
    application_id: int | None
    attempts: int
    max_attempts: int
    error: str | None
    result_ref: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: int | None
    # Outcome of the run: the latest RCA report status or repair attempt status.
    result: str | None
    stop_reason: str | None
    # AI provider used by the run (``mock`` for the deterministic mock).
    provider: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None


class RepairAttemptOut(BaseModel):
    id: int
    incident_id: int
    job_id: int | None
    repository_id: int | None
    attempt_number: int
    branch_name: str | None
    status: str
    explanation: str | None
    diff: str | None
    test_command: list[str] | None
    test_output: str | None
    test_summary: dict[str, Any] | None
    returncode: int | None
    tests_passed: bool | None
    regression_test_failed_before: bool | None
    commit_sha: str | None
    stop_reason: str | None
    error: str | None
    pr_url: str | None
    duration_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    created_at: datetime
    finished_at: datetime | None


class RunPage(BaseModel):
    items: list[RunOut]
    total: int
    limit: int
    offset: int


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _duration_ms(job: Job) -> int | None:
    if job.started_at is None or job.finished_at is None:
        return None
    delta = _as_utc(job.finished_at) - _as_utc(job.started_at)
    return max(int(delta.total_seconds() * 1000), 0)


def _sum(values: list[Any]) -> Any:
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _run_details(db: Session, jobs: list[Job]) -> dict[int, dict[str, Any]]:
    """Result, stop reason, tokens and cost of each run (``None`` when unknown)."""
    ids = [job.id for job in jobs]
    rows: dict[int, list[Any]] = {job_id: [] for job_id in ids}
    providers: dict[int, str | None] = {}
    if ids:
        for report in db.scalars(
            select(RCAReport).where(RCAReport.job_id.in_(ids)).order_by(RCAReport.id)
        ):
            rows[report.job_id].append(report)
            providers[report.job_id] = report.provider
        for attempt in db.scalars(
            select(RepairAttempt).where(RepairAttempt.job_id.in_(ids)).order_by(RepairAttempt.id)
        ):
            rows[attempt.job_id].append(attempt)
        # Repair attempts do not store the provider; the repair engine records
        # it in the ``repair_started`` audit entry of each job.
        incident_ids = {job.incident_id for job in jobs if job.type == "repair" and job.incident_id}
        if incident_ids:
            for entry in db.scalars(
                select(AuditLog).where(
                    AuditLog.action == "repair_started", AuditLog.incident_id.in_(incident_ids)
                )
            ):
                job_id = (entry.details or {}).get("job_id")
                if job_id in rows:
                    providers[job_id] = (entry.details or {}).get("provider")
    details: dict[int, dict[str, Any]] = {}
    for job_id, items in rows.items():
        last = items[-1] if items else None
        details[job_id] = {
            "result": last.status if last is not None else None,
            "stop_reason": getattr(last, "stop_reason", None),
            "provider": providers.get(job_id),
            "input_tokens": _sum([item.input_tokens for item in items]),
            "output_tokens": _sum([item.output_tokens for item in items]),
            "cost_usd": _sum([item.cost_usd for item in items]),
        }
    return details


def _repair_out(attempt: RepairAttempt) -> dict:
    return RepairAttemptOut(
        id=attempt.id,
        incident_id=attempt.incident_id,
        job_id=attempt.job_id,
        repository_id=attempt.repository_id,
        attempt_number=attempt.attempt_number,
        branch_name=attempt.branch_name,
        status=attempt.status,
        explanation=redact_text(attempt.explanation) if attempt.explanation else attempt.explanation,
        diff=redact_text(attempt.diff) if attempt.diff else attempt.diff,
        test_command=attempt.test_command,
        test_output=redact_text(attempt.test_output) if attempt.test_output else attempt.test_output,
        test_summary=attempt.test_summary,
        returncode=attempt.returncode,
        tests_passed=attempt.tests_passed,
        regression_test_failed_before=attempt.regression_test_failed_before,
        commit_sha=attempt.commit_sha,
        stop_reason=attempt.stop_reason,
        error=redact_text(attempt.error) if attempt.error else attempt.error,
        pr_url=attempt.pr_url,
        duration_ms=attempt.duration_ms,
        input_tokens=attempt.input_tokens,
        output_tokens=attempt.output_tokens,
        cost_usd=attempt.cost_usd,
        created_at=attempt.created_at,
        finished_at=attempt.finished_at,
    ).model_dump()


def _run_out(job: Job, incident_title: str | None, details: dict[str, Any]) -> dict:
    return RunOut(
        id=job.id,
        type=job.type,
        status=job.status,
        incident_id=job.incident_id,
        incident_title=redact_text(incident_title) if incident_title else incident_title,
        application_id=job.application_id,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        error=redact_text(job.error) if job.error else job.error,
        result_ref=job.result_ref,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        duration_ms=_duration_ms(job),
        **details,
    ).model_dump()


def _process_inline(request: Request, clock: Clock) -> None:
    from app.worker import run_once

    settings: Settings = request.app.state.settings
    run_once(
        request.app.state.session_factory,
        request.app.state.job_handlers,
        clock=clock,
        job_timeout_seconds=settings.job_timeout_seconds,
        backoff_seconds=settings.job_retry_backoff_seconds,
    )


def jobs_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_admin)])

    @router.post(
        "/incidents/{incident_id}/rca",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=JobAccepted,
    )
    def request_rca(
        incident_id: int,
        request: Request,
        db: Session = Depends(get_db),
        clock: Clock = Depends(get_clock),
    ) -> dict:
        incident = _get_incident(db, incident_id)
        existing = queue.active_job(db, "rca", incident.id)
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"An RCA job for this incident is already {existing.status} (job {existing.id})",
            )
        settings: Settings = request.app.state.settings
        job = queue.enqueue(
            db,
            "rca",
            clock,
            incident_id=incident.id,
            application_id=incident.application_id,
            max_attempts=settings.job_max_attempts,
        )
        db.add(
            AuditLog(
                application_id=incident.application_id,
                incident_id=incident.id,
                actor="admin",
                action="rca_requested",
                details={"job_id": job.id},
                created_at=clock.now(),
            )
        )
        db.commit()
        if settings.run_jobs_inline:
            _process_inline(request, clock)
            job = db.get(Job, job.id, populate_existing=True)
        return {"job_id": job.id, "status": job.status}

    @router.get("/incidents/{incident_id}/rca", response_model=RCAReportOut)
    def latest_rca(incident_id: int, db: Session = Depends(get_db)) -> dict:
        incident = _get_incident(db, incident_id)
        report = db.scalar(
            select(RCAReport)
            .where(RCAReport.incident_id == incident.id)
            .order_by(RCAReport.created_at.desc(), RCAReport.id.desc())
            .limit(1)
        )
        if report is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="No RCA report for this incident"
            )
        return RCAReportOut(
            id=report.id,
            incident_id=report.incident_id,
            job_id=report.job_id,
            status=report.status,
            summary=redact_text(report.summary or ""),
            report=redact_obj(report.report or {}),
            provider=report.provider,
            model=report.model,
            is_mock=report.provider == "mock",
            input_tokens=report.input_tokens,
            output_tokens=report.output_tokens,
            cost_usd=report.cost_usd,
            error=redact_text(report.error) if report.error else report.error,
            duration_ms=report.duration_ms,
            created_at=report.created_at,
            finished_at=report.finished_at,
        ).model_dump()

    @router.post(
        "/incidents/{incident_id}/repair",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=JobAccepted,
    )
    def request_repair(
        incident_id: int,
        request: Request,
        db: Session = Depends(get_db),
        clock: Clock = Depends(get_clock),
    ) -> dict:
        incident = _get_incident(db, incident_id)
        if latest_completed_rca(db, incident.id) is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A completed RCA is required before requesting a repair",
            )
        if repair_repository_for(db, incident.application_id) is None:
            raise HTTPException(
                status_code=422,
                detail="The application has no authorized repository",
            )
        existing = queue.active_job(db, "repair", incident.id)
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A repair job for this incident is already {existing.status} (job {existing.id})",
            )
        settings: Settings = request.app.state.settings
        # The repair engine retries internally (max_repair_attempts): one job run.
        job = queue.enqueue(
            db,
            "repair",
            clock,
            incident_id=incident.id,
            application_id=incident.application_id,
            max_attempts=1,
        )
        db.add(
            AuditLog(
                application_id=incident.application_id,
                incident_id=incident.id,
                actor="admin",
                action="repair_requested",
                details={"job_id": job.id},
                created_at=clock.now(),
            )
        )
        db.commit()
        if settings.run_jobs_inline:
            _process_inline(request, clock)
            job = db.get(Job, job.id, populate_existing=True)
        return {"job_id": job.id, "status": job.status}

    @router.get("/incidents/{incident_id}/repairs", response_model=list[RepairAttemptOut])
    def list_repairs(incident_id: int, db: Session = Depends(get_db)) -> list[dict]:
        incident = _get_incident(db, incident_id)
        attempts = db.scalars(
            select(RepairAttempt)
            .where(RepairAttempt.incident_id == incident.id)
            .order_by(RepairAttempt.created_at, RepairAttempt.id)
        ).all()
        return [_repair_out(attempt) for attempt in attempts]

    @router.get("/runs", response_model=RunPage)
    def list_runs(
        job_type: JobType | None = Query(default=None, alias="type"),
        status_filter: JobStatus | None = Query(default=None, alias="status"),
        incident_id: int | None = None,
        limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
    ) -> dict:
        conditions = []
        if job_type is not None:
            conditions.append(Job.type == job_type)
        if status_filter is not None:
            conditions.append(Job.status == status_filter)
        if incident_id is not None:
            conditions.append(Job.incident_id == incident_id)

        total = db.scalar(select(func.count()).select_from(Job).where(*conditions)) or 0
        rows = db.execute(
            select(Job, Incident.title)
            .outerjoin(Incident, Incident.id == Job.incident_id)
            .where(*conditions)
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        details = _run_details(db, [job for job, _ in rows])
        return {
            "items": [_run_out(job, title, details[job.id]) for job, title in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    return router
