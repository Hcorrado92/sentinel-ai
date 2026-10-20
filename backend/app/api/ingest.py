"""Ingestion endpoint: ``POST /api/v1/ingest/events``.

Order of checks: ingest key (401), body size (413, from Content-Length and
from the bytes actually received, before parsing), schema and limits (422),
key/application match (403). Nothing is persisted unless all of them pass.
"""

from dataclasses import dataclass

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.clock import Clock
from app.config import Settings
from app.ingestion import ingest_event
from app.models import Application
from app.schemas.ingest import IngestAccepted, IngestEvent, IngestLimits
from app.security.auth import hash_ingest_key, verify_ingest_key


@dataclass(frozen=True)
class _Caller:
    application_id: int
    slug: str


def _authenticate(session_factory: sessionmaker[Session], key: str | None) -> _Caller:
    if key:
        with session_factory() as db:
            application = db.scalar(
                select(Application).where(Application.ingest_key_hash == hash_ingest_key(key))
            )
            if application is not None and verify_ingest_key(key, application.ingest_key_hash):
                return _Caller(application_id=application.id, slug=application.slug)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing ingest key"
    )


def _too_large(limit: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail=f"Request body is larger than {limit} bytes",
    )


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_length = int(declared)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Content-Length header"
            ) from None
        if declared_length > limit:
            raise _too_large(limit)

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > limit:
            raise _too_large(limit)
    return bytes(body)


def _parse(body: bytes, limits: IngestLimits) -> IngestEvent:
    try:
        return IngestEvent.model_validate_json(body, context={"limits": limits})
    except ValidationError as exc:
        # Inputs are not echoed back: they may contain unredacted secrets.
        raise RequestValidationError(
            exc.errors(include_url=False, include_context=False, include_input=False)
        ) from None


def ingest_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/ingest")

    @router.post(
        "/events",
        status_code=status.HTTP_202_ACCEPTED,
        response_model=IngestAccepted,
    )
    async def ingest_events(
        request: Request,
        x_sentinel_key: str | None = Header(default=None),
    ) -> dict:
        settings: Settings = request.app.state.settings
        session_factory: sessionmaker[Session] = request.app.state.session_factory
        clock: Clock = request.app.state.clock

        caller = await run_in_threadpool(_authenticate, session_factory, x_sentinel_key)
        body = await _read_body(request, settings.max_body_bytes)
        payload = _parse(body, IngestLimits.from_settings(settings))
        if payload.application != caller.slug:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Ingest key does not belong to this application",
            )

        def _store() -> dict:
            with session_factory() as db:
                result = ingest_event(db, caller.application_id, payload, clock)
            return {
                "event_id": result.event_id,
                "incident_id": result.incident_id,
                "deduplicated": result.deduplicated,
            }

        return await run_in_threadpool(_store)

    return router
