"""Draft pull request endpoints.

``POST /incidents/{id}/pull-request`` prepares the PR for the latest
successful repair attempt (201), or answers 409 and records ``pr_refused``
when there is none. Publication through ``gh`` only happens when
``SENTINEL_GITHUB_PUBLISH=true`` (see ``app.github.pr``).
``GET /incidents/{id}/pull-request`` returns the latest PR (404 when none).
Both endpoints require the admin bearer token.
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dashboard import _get_incident
from app.clock import Clock, get_clock
from app.config import Settings
from app.db import get_db
from app.github.pr import PRNotAllowed, prepare_pull_request
from app.jobs.handlers import latest_completed_rca
from app.models import AuthorizedRepository, PullRequest, RepairAttempt
from app.runner import AllowListRunner
from app.security.auth import require_admin
from app.security.redaction import redact_text


class PullRequestOut(BaseModel):
    id: int
    incident_id: int
    repair_attempt_id: int
    repository_id: int | None
    status: str
    branch: str
    base: str
    title: str
    body: str
    github_repo: str | None
    url: str | None
    error: str | None
    draft: bool
    created_at: datetime
    published_at: datetime | None


def _pr_out(pr: PullRequest) -> dict:
    return PullRequestOut(
        id=pr.id,
        incident_id=pr.incident_id,
        repair_attempt_id=pr.repair_attempt_id,
        repository_id=pr.repository_id,
        status=pr.status,
        branch=pr.branch,
        base=pr.base,
        # Stored redacted (app.github.pr); redacted again as defence in depth.
        title=redact_text(pr.title),
        body=redact_text(pr.body),
        github_repo=pr.github_repo,
        url=pr.url,
        error=redact_text(pr.error) if pr.error else pr.error,
        draft=True,
        created_at=pr.created_at,
        published_at=pr.published_at,
    ).model_dump()


def _candidate_attempt(db: Session, incident_id: int) -> RepairAttempt | None:
    """The latest successful attempt, else the latest attempt (to explain a refusal)."""
    ordering = (RepairAttempt.created_at.desc(), RepairAttempt.id.desc())
    succeeded = db.scalar(
        select(RepairAttempt)
        .where(RepairAttempt.incident_id == incident_id, RepairAttempt.status == "succeeded")
        .order_by(*ordering)
        .limit(1)
    )
    if succeeded is not None:
        return succeeded
    return db.scalar(
        select(RepairAttempt)
        .where(RepairAttempt.incident_id == incident_id)
        .order_by(*ordering)
        .limit(1)
    )


def pull_requests_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_admin)])

    @router.post(
        "/incidents/{incident_id}/pull-request",
        status_code=status.HTTP_201_CREATED,
        response_model=PullRequestOut,
    )
    def create_pull_request(
        incident_id: int,
        request: Request,
        db: Session = Depends(get_db),
        clock: Clock = Depends(get_clock),
    ) -> dict:
        incident = _get_incident(db, incident_id)
        settings: Settings = request.app.state.settings
        attempt = _candidate_attempt(db, incident.id)
        repo = (
            db.get(AuthorizedRepository, attempt.repository_id)
            if attempt is not None and attempt.repository_id is not None
            else None
        )
        try:
            pr = prepare_pull_request(
                attempt,
                incident,
                latest_completed_rca(db, incident.id),
                repo,
                AllowListRunner(request.app.state.git_runner),
                settings,
                db=db,
                clock=clock,
            )
        except PRNotAllowed as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A successful repair is required before preparing a pull request: {exc}",
            ) from exc
        return _pr_out(pr)

    @router.get("/incidents/{incident_id}/pull-request", response_model=PullRequestOut)
    def get_pull_request(incident_id: int, db: Session = Depends(get_db)) -> dict:
        incident = _get_incident(db, incident_id)
        pr = db.scalar(
            select(PullRequest)
            .where(PullRequest.incident_id == incident.id)
            .order_by(PullRequest.created_at.desc(), PullRequest.id.desc())
            .limit(1)
        )
        if pr is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="No pull request for this incident"
            )
        return _pr_out(pr)

    return router
