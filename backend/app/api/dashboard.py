"""Dashboard read endpoints: applications, incidents, events, timeline and audit log.

All endpoints require the admin bearer token. Events are stored already
redacted (``app.ingestion``), so they are returned as persisted.
"""

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    INCIDENT_STATUSES,
    Application,
    AuditLog,
    Event,
    Incident,
    RCAReport,
    RepairAttempt,
)
from app.security.auth import require_admin

# Audit actions shown in the incident timeline; ``rca_*``, ``repair_*`` and
# ``pr_*`` actions are included by prefix.
TIMELINE_ACTIONS = frozenset({"event_received", "incident_grouped", "incident_regressed"})
TIMELINE_PREFIXES = ("rca_", "repair_", "pr_")

MAX_PAGE_SIZE = 200

IncidentStatus = Literal["open", "investigating", "fix_proposed", "resolved", "regressed"]
Severity = Literal["low", "medium", "high", "critical"]


class ApplicationSummary(BaseModel):
    id: int
    name: str
    slug: str
    created_at: datetime
    budget_usd: float | None
    monthly_spend_usd: float
    incident_counts: dict[str, int]
    total_incidents: int
    # Sum of the AI costs reported for RCA and repair runs; ``None`` ("not
    # available") when no provider reported a cost, e.g. under the mock.
    ai_cost_usd: float | None


class IncidentOut(BaseModel):
    id: int
    application_id: int
    application_name: str
    fingerprint: str
    title: str
    exception_type: str
    severity: str
    status: str
    event_count: int
    first_seen: datetime
    last_seen: datetime
    resolved_at: datetime | None
    created_at: datetime


class IncidentPage(BaseModel):
    items: list[IncidentOut]
    total: int
    limit: int
    offset: int


class IncidentDetail(IncidentOut):
    latest_rca_status: str | None
    latest_repair_status: str | None


class EventOut(BaseModel):
    id: int
    event_id: str | None
    timestamp: datetime
    received_at: datetime
    exception_type: str
    message: str
    stack_trace: str
    environment: str
    correlation_id: str | None
    metadata: dict[str, Any]


class AuditEntry(BaseModel):
    id: int
    actor: str
    action: str
    details: dict[str, Any]
    created_at: datetime


class TimelineEntry(BaseModel):
    id: int
    type: str
    timestamp: datetime
    actor: str
    details: dict[str, Any]


def _incident_out(incident: Incident, application_name: str) -> dict:
    return IncidentOut(
        id=incident.id,
        application_id=incident.application_id,
        application_name=application_name,
        fingerprint=incident.fingerprint,
        title=incident.title,
        exception_type=incident.exception_type,
        severity=incident.severity,
        status=incident.status,
        event_count=incident.event_count,
        first_seen=incident.first_seen,
        last_seen=incident.last_seen,
        resolved_at=incident.resolved_at,
        created_at=incident.created_at,
    ).model_dump()


def _get_incident(db: Session, incident_id: int) -> Incident:
    incident = db.get(Incident, incident_id)
    if incident is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Incident not found")
    return incident


def _audit_entries(db: Session, incident_id: int) -> list[AuditLog]:
    return list(
        db.scalars(
            select(AuditLog)
            .where(AuditLog.incident_id == incident_id)
            .order_by(AuditLog.created_at, AuditLog.id)
        ).all()
    )


def _in_timeline(action: str) -> bool:
    return action in TIMELINE_ACTIONS or action.startswith(TIMELINE_PREFIXES)


def dashboard_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_admin)])

    @router.get("/applications", response_model=list[ApplicationSummary])
    def list_applications(db: Session = Depends(get_db)) -> list[dict]:
        applications = db.scalars(select(Application).order_by(Application.id)).all()
        rows = db.execute(
            select(Incident.application_id, Incident.status, func.count())
            .group_by(Incident.application_id, Incident.status)
        ).all()
        counts: dict[int, dict[str, int]] = {}
        for application_id, incident_status, count in rows:
            counts.setdefault(application_id, {})[incident_status] = count
        costs: dict[int, float] = {}
        for model in (RCAReport, RepairAttempt):
            # SUM ignores NULLs and is NULL when no cost was reported.
            cost_rows = db.execute(
                select(Incident.application_id, func.sum(model.cost_usd))
                .select_from(model)
                .join(Incident, Incident.id == model.incident_id)
                .group_by(Incident.application_id)
            ).all()
            for application_id, total in cost_rows:
                if total is not None:
                    costs[application_id] = costs.get(application_id, 0.0) + float(total)

        result = []
        for application in applications:
            by_status = {s: 0 for s in INCIDENT_STATUSES}
            by_status.update(counts.get(application.id, {}))
            result.append(
                ApplicationSummary(
                    id=application.id,
                    name=application.name,
                    slug=application.slug,
                    created_at=application.created_at,
                    budget_usd=application.budget_usd,
                    monthly_spend_usd=application.monthly_spend_usd,
                    incident_counts=by_status,
                    total_incidents=sum(by_status.values()),
                    ai_cost_usd=costs.get(application.id),
                ).model_dump()
            )
        return result

    @router.get("/incidents", response_model=IncidentPage)
    def list_incidents(
        application_id: int | None = None,
        status_filter: IncidentStatus | None = Query(default=None, alias="status"),
        severity: Severity | None = None,
        order: Literal["asc", "desc"] = "desc",
        limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
    ) -> dict:
        conditions = []
        if application_id is not None:
            conditions.append(Incident.application_id == application_id)
        if status_filter is not None:
            conditions.append(Incident.status == status_filter)
        if severity is not None:
            conditions.append(Incident.severity == severity)

        total = db.scalar(select(func.count()).select_from(Incident).where(*conditions)) or 0
        sort = Incident.last_seen.desc() if order == "desc" else Incident.last_seen.asc()
        tie = Incident.id.desc() if order == "desc" else Incident.id.asc()
        rows = db.execute(
            select(Incident, Application.name)
            .join(Application, Application.id == Incident.application_id)
            .where(*conditions)
            .order_by(sort, tie)
            .limit(limit)
            .offset(offset)
        ).all()
        return {
            "items": [_incident_out(incident, name) for incident, name in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    @router.get("/incidents/{incident_id}", response_model=IncidentDetail)
    def get_incident(incident_id: int, db: Session = Depends(get_db)) -> dict:
        incident = _get_incident(db, incident_id)
        application = db.get(Application, incident.application_id)
        latest_rca = db.scalar(
            select(RCAReport.status)
            .where(RCAReport.incident_id == incident.id)
            .order_by(RCAReport.created_at.desc(), RCAReport.id.desc())
            .limit(1)
        )
        latest_repair = db.scalar(
            select(RepairAttempt.status)
            .where(RepairAttempt.incident_id == incident.id)
            .order_by(RepairAttempt.created_at.desc(), RepairAttempt.id.desc())
            .limit(1)
        )
        return {
            **_incident_out(incident, application.name if application else ""),
            "latest_rca_status": latest_rca,
            "latest_repair_status": latest_repair,
        }

    @router.get("/incidents/{incident_id}/events", response_model=list[EventOut])
    def list_events(
        incident_id: int,
        limit: int = Query(default=50, ge=1, le=MAX_PAGE_SIZE),
        offset: int = Query(default=0, ge=0),
        db: Session = Depends(get_db),
    ) -> list[dict]:
        incident = _get_incident(db, incident_id)
        events = db.scalars(
            select(Event)
            .where(Event.incident_id == incident.id)
            .order_by(Event.timestamp.desc(), Event.id.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return [
            EventOut(
                id=event.id,
                event_id=event.event_id,
                timestamp=event.timestamp,
                received_at=event.received_at,
                exception_type=event.exception_type,
                message=event.message,
                stack_trace=event.stack_trace,
                environment=event.environment,
                correlation_id=event.correlation_id,
                metadata=event.metadata_json or {},
            ).model_dump()
            for event in events
        ]

    @router.get("/incidents/{incident_id}/timeline", response_model=list[TimelineEntry])
    def get_timeline(incident_id: int, db: Session = Depends(get_db)) -> list[dict]:
        incident = _get_incident(db, incident_id)
        return [
            TimelineEntry(
                id=entry.id,
                type=entry.action,
                timestamp=entry.created_at,
                actor=entry.actor,
                details=entry.details or {},
            ).model_dump()
            for entry in _audit_entries(db, incident.id)
            if _in_timeline(entry.action)
        ]

    @router.get("/incidents/{incident_id}/audit", response_model=list[AuditEntry])
    def get_audit(incident_id: int, db: Session = Depends(get_db)) -> list[dict]:
        incident = _get_incident(db, incident_id)
        return [
            AuditEntry(
                id=entry.id,
                actor=entry.actor,
                action=entry.action,
                details=entry.details or {},
                created_at=entry.created_at,
            ).model_dump()
            for entry in _audit_entries(db, incident.id)
        ]

    return router
