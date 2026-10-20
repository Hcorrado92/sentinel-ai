"""Persistence of ingested events: redaction, dedup and incident grouping.

Events are grouped by (application, fingerprint), where the fingerprint comes
from the exception type and the normalized stack-trace frames
(``app.ingest.fingerprint``). Severity is recomputed on every new event
(``app.ingest.severity``). A resolved incident that receives a new event is
reopened with status ``regressed``.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clock import Clock
from app.ingest.fingerprint import compute_fingerprint
from app.ingest.severity import compute_severity, is_production
from app.models import AuditLog, Event, Incident
from app.schemas.ingest import IngestEvent
from app.security.redaction import redact_obj, redact_text

INGEST_ACTOR = "ingest"
TITLE_MAX_LENGTH = 300


@dataclass(frozen=True)
class IngestResult:
    event_id: str
    incident_id: int
    deduplicated: bool


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _title(exception_type: str, message: str) -> str:
    first_line = message.strip().splitlines()[0] if message.strip() else ""
    title = f"{exception_type}: {first_line}" if first_line else exception_type
    return title[:TITLE_MAX_LENGTH]


def _find_event(db: Session, application_id: int, event_id: str) -> Event | None:
    return db.scalar(
        select(Event).where(Event.application_id == application_id, Event.event_id == event_id)
    )


def _audit(db: Session, clock: Clock, application_id: int, result: IngestResult) -> None:
    db.add(
        AuditLog(
            application_id=application_id,
            incident_id=result.incident_id,
            actor=INGEST_ACTOR,
            action="event_received",
            details={"event_id": result.event_id, "deduplicated": result.deduplicated},
            created_at=clock.now(),
        )
    )


def _seen_in_production(db: Session, incident: Incident, environment: str) -> bool:
    if is_production(environment):
        return True
    if incident.id is None:
        return False
    environments = db.scalars(
        select(Event.environment).where(Event.incident_id == incident.id).distinct()
    )
    return any(is_production(env) for env in environments)


def _attach_incident(
    db: Session,
    application_id: int,
    payload: IngestEvent,
    message: str,
    event_id: str,
    now: datetime,
) -> Incident:
    fingerprint = compute_fingerprint(payload.exception_type, payload.stack_trace)
    incident = db.scalar(
        select(Incident).where(
            Incident.application_id == application_id, Incident.fingerprint == fingerprint
        )
    )
    seen = payload.timestamp
    if incident is None:
        incident = Incident(
            application_id=application_id,
            fingerprint=fingerprint,
            title=_title(payload.exception_type, message),
            exception_type=payload.exception_type,
            severity="low",
            status="open",
            event_count=0,
            first_seen=seen,
            last_seen=seen,
            created_at=now,
        )
        db.add(incident)
    elif incident.status == "resolved":
        incident.status = "regressed"
        db.add(
            AuditLog(
                application_id=application_id,
                incident_id=incident.id,
                actor=INGEST_ACTOR,
                action="incident_regressed",
                details={
                    "event_id": event_id,
                    "previous_status": "resolved",
                    "resolved_at": incident.resolved_at.isoformat()
                    if incident.resolved_at
                    else None,
                },
                created_at=now,
            )
        )
        incident.resolved_at = None

    production = _seen_in_production(db, incident, payload.environment)
    incident.event_count += 1
    if seen < _as_utc(incident.first_seen):
        incident.first_seen = seen
    if seen > _as_utc(incident.last_seen):
        incident.last_seen = seen
    incident.severity = compute_severity(
        incident.exception_type, production, incident.event_count
    )
    db.flush()
    return incident


def _deduplicated(db: Session, clock: Clock, application_id: int, event: Event) -> IngestResult:
    result = IngestResult(
        event_id=event.event_id or "", incident_id=event.incident_id or 0, deduplicated=True
    )
    _audit(db, clock, application_id, result)
    db.commit()
    return result


def _store(
    db: Session, application_id: int, payload: IngestEvent, event_id: str, clock: Clock
) -> IngestResult:
    message = redact_text(payload.message)
    now = clock.now()
    incident = _attach_incident(db, application_id, payload, message, event_id, now)
    event = Event(
        application_id=application_id,
        incident_id=incident.id,
        event_id=event_id,
        timestamp=payload.timestamp,
        exception_type=payload.exception_type,
        message=message,
        stack_trace=redact_text(payload.stack_trace),
        environment=payload.environment,
        correlation_id=payload.correlation_id,
        metadata_json=redact_obj(dict(payload.metadata)),
        received_at=now,
    )
    db.add(event)
    result = IngestResult(event_id=event_id, incident_id=incident.id, deduplicated=False)
    db.flush()
    _audit(db, clock, application_id, result)
    db.add(
        AuditLog(
            application_id=application_id,
            incident_id=incident.id,
            actor=INGEST_ACTOR,
            action="incident_grouped",
            details={
                "event_id": event_id,
                "fingerprint": incident.fingerprint,
                "event_count": incident.event_count,
                "new_incident": incident.event_count == 1,
                "severity": incident.severity,
            },
            created_at=now,
        )
    )
    db.commit()
    return result


def ingest_event(
    db: Session, application_id: int, payload: IngestEvent, clock: Clock
) -> IngestResult:
    """Store a validated event for ``application_id`` (already authorized)."""
    if payload.event_id is not None:
        existing = _find_event(db, application_id, payload.event_id)
        if existing is not None:
            return _deduplicated(db, clock, application_id, existing)

    event_id = payload.event_id or uuid.uuid4().hex
    try:
        return _store(db, application_id, payload, event_id, clock)
    except IntegrityError:
        # A concurrent request stored the same event id or created the same
        # incident first: dedup, or retry once against the existing incident.
        db.rollback()
        existing = _find_event(db, application_id, event_id)
        if existing is not None:
            return _deduplicated(db, clock, application_id, existing)
        return _store(db, application_id, payload, event_id, clock)
