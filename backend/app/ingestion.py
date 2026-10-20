"""Persistence of ingested events: redaction, dedup and incident grouping.

Grouping is provisional: events are attached to an incident keyed by
(application, exception_type). Stack-trace fingerprinting replaces it later.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clock import Clock
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


def provisional_fingerprint(exception_type: str) -> str:
    return hashlib.sha256(f"provisional:{exception_type}".encode("utf-8")).hexdigest()


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


def _attach_incident(
    db: Session, application_id: int, payload: IngestEvent, message: str, now: datetime
) -> Incident:
    fingerprint = provisional_fingerprint(payload.exception_type)
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
            severity="medium",
            status="open",
            event_count=0,
            first_seen=seen,
            last_seen=seen,
            created_at=now,
        )
        db.add(incident)
    incident.event_count += 1
    if seen < _as_utc(incident.first_seen):
        incident.first_seen = seen
    if seen > _as_utc(incident.last_seen):
        incident.last_seen = seen
    db.flush()
    return incident


def _deduplicated(db: Session, clock: Clock, application_id: int, event: Event) -> IngestResult:
    result = IngestResult(
        event_id=event.event_id or "", incident_id=event.incident_id or 0, deduplicated=True
    )
    _audit(db, clock, application_id, result)
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
    message = redact_text(payload.message)
    now = clock.now()
    incident = _attach_incident(db, application_id, payload, message, now)
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
    try:
        db.flush()
        _audit(db, clock, application_id, result)
        db.commit()
    except IntegrityError:
        # A concurrent request stored the same event id first.
        db.rollback()
        existing = _find_event(db, application_id, event_id)
        if existing is None:
            raise
        return _deduplicated(db, clock, application_id, existing)
    return result
