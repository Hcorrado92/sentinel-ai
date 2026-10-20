"""RCA job endpoints and the run history.

``POST /incidents/{id}/rca`` enqueues an RCA job (202, or 409 when one is
already queued/running). With ``SENTINEL_RUN_JOBS_INLINE=true`` the job is
processed before responding. All endpoints require the admin bearer token.
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
from app.models import AuditLog, Incident, Job, RCAReport
from app.security.auth import require_admin

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


def _run_out(job: Job, incident_title: str | None) -> dict:
    return RunOut(
        id=job.id,
        type=job.type,
        status=job.status,
        incident_id=job.incident_id,
        incident_title=incident_title,
        application_id=job.application_id,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        error=job.error,
        result_ref=job.result_ref,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        duration_ms=_duration_ms(job),
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
            summary=report.summary,
            report=report.report or {},
            provider=report.provider,
            model=report.model,
            is_mock=report.provider == "mock",
            input_tokens=report.input_tokens,
            output_tokens=report.output_tokens,
            cost_usd=report.cost_usd,
            error=report.error,
            duration_ms=report.duration_ms,
            created_at=report.created_at,
            finished_at=report.finished_at,
        ).model_dump()

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
        return {
            "items": [_run_out(job, title) for job, title in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    return router
