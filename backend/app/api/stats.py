"""Event frequency statistics for the dashboard charts.

``GET /incidents/{id}/stats`` and ``GET /stats/events`` return event counts
per time bucket (``hour`` or ``day``, by event timestamp in UTC). Only the
timestamps are selected with portable SQL; bucketing happens in Python so the
same code works on SQLite and PostgreSQL. Buckets between the first and the
last event are filled with zero counts, keeping the most recent
``MAX_BUCKETS``. Both endpoints require the admin bearer token.
"""

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dashboard import _get_incident
from app.db import get_db
from app.models import Event
from app.security.auth import require_admin

MAX_BUCKETS = 500

Bucket = Literal["hour", "day"]
BUCKET_SIZES: dict[str, timedelta] = {"hour": timedelta(hours=1), "day": timedelta(days=1)}


class BucketCount(BaseModel):
    start: datetime
    count: int


class EventStats(BaseModel):
    bucket: str
    incident_id: int | None
    application_id: int | None
    total: int
    buckets: list[BucketCount]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _floor(value: datetime, bucket: str) -> datetime:
    value = _as_utc(value).replace(minute=0, second=0, microsecond=0)
    return value.replace(hour=0) if bucket == "day" else value


def bucket_counts(timestamps: list[datetime], bucket: str) -> list[dict]:
    """Count ``timestamps`` per bucket, zero-filling gaps between the first and last."""
    counts = Counter(_floor(ts, bucket) for ts in timestamps)
    if not counts:
        return []
    step = BUCKET_SIZES[bucket]
    first, last = min(counts), max(counts)
    first = max(first, last - step * (MAX_BUCKETS - 1))
    result = []
    current = first
    while current <= last:
        result.append({"start": current, "count": counts.get(current, 0)})
        current += step
    return result


def _event_stats(
    db: Session,
    bucket: str,
    *,
    incident_id: int | None = None,
    application_id: int | None = None,
) -> dict:
    conditions = []
    if incident_id is not None:
        conditions.append(Event.incident_id == incident_id)
    if application_id is not None:
        conditions.append(Event.application_id == application_id)
    timestamps = list(db.scalars(select(Event.timestamp).where(*conditions)).all())
    buckets = bucket_counts(timestamps, bucket)
    return EventStats(
        bucket=bucket,
        incident_id=incident_id,
        application_id=application_id,
        total=sum(b["count"] for b in buckets),
        buckets=[BucketCount(**b) for b in buckets],
    ).model_dump()


def stats_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_admin)])

    @router.get("/incidents/{incident_id}/stats", response_model=EventStats)
    def incident_stats(
        incident_id: int,
        bucket: Bucket = Query(default="hour"),
        db: Session = Depends(get_db),
    ) -> dict:
        incident = _get_incident(db, incident_id)
        return _event_stats(db, bucket, incident_id=incident.id)

    @router.get("/stats/events", response_model=EventStats)
    def event_stats(
        bucket: Bucket = Query(default="hour"),
        application_id: int | None = None,
        incident_id: int | None = None,
        db: Session = Depends(get_db),
    ) -> dict:
        return _event_stats(db, bucket, incident_id=incident_id, application_id=application_id)

    return router
