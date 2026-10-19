"""Admin endpoints: applications, ingest keys and authorized repositories."""

import re
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clock import Clock, get_clock
from app.db import get_db
from app.models import Application, AuditLog, AuthorizedRepository
from app.security.auth import generate_ingest_key, hash_ingest_key, require_admin

ADMIN_ACTOR = "admin"

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
ArgvItem = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=1000)]


class ApplicationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: NonEmptyStr
    budget_usd: float | None = Field(default=None, ge=0)


class ApplicationOut(BaseModel):
    """Public view of an application: never includes the key or its hash."""

    id: int
    name: str
    slug: str
    created_at: datetime
    budget_usd: float | None
    monthly_spend_usd: float


class ApplicationWithKey(ApplicationOut):
    ingest_key: str


class RotatedKey(BaseModel):
    id: int
    ingest_key: str


class RepositoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: NonEmptyStr
    local_path: str | None = Field(default=None, min_length=1, max_length=4096)
    clone_url: str | None = Field(default=None, min_length=1, max_length=4096)
    base_branch: NonEmptyStr = "main"
    test_command: list[ArgvItem] = Field(min_length=1, max_length=100)
    github_repo: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=200
    )

    @field_validator("test_command", mode="before")
    @classmethod
    def _require_argv_list(cls, value: object) -> object:
        if not isinstance(value, list):
            raise ValueError("test_command must be an argv list of strings, not a shell string")
        return value

    @model_validator(mode="after")
    def _at_least_one_source(self) -> "RepositoryCreate":
        if self.local_path is None and self.clone_url is None:
            raise ValueError("either local_path or clone_url is required")
        return self


class RepositoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    application_id: int
    name: str
    local_path: str | None
    clone_url: str | None
    base_branch: str
    test_command: list[str]
    github_repo: str | None
    created_at: datetime


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "app"


def _app_out(application: Application) -> dict:
    return ApplicationOut(
        id=application.id,
        name=application.name,
        slug=application.slug,
        created_at=application.created_at,
        budget_usd=application.budget_usd,
        monthly_spend_usd=application.monthly_spend_usd,
    ).model_dump()


def _get_application(db: Session, application_id: int) -> Application:
    application = db.get(Application, application_id)
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
    return application


def _audit(db: Session, clock: Clock, application_id: int, action: str, details: dict) -> None:
    db.add(
        AuditLog(
            application_id=application_id,
            actor=ADMIN_ACTOR,
            action=action,
            details=details,
            created_at=clock.now(),
        )
    )


def admin_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/admin", dependencies=[Depends(require_admin)])

    @router.post(
        "/applications",
        status_code=status.HTTP_201_CREATED,
        response_model=ApplicationWithKey,
    )
    def create_application(
        payload: ApplicationCreate,
        db: Session = Depends(get_db),
        clock: Clock = Depends(get_clock),
    ) -> dict:
        slug = _slugify(payload.name)
        conflict = db.scalar(
            select(Application).where(
                (Application.name == payload.name) | (Application.slug == slug)
            )
        )
        if conflict is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Application already exists"
            )

        key = generate_ingest_key()
        application = Application(
            name=payload.name,
            slug=slug,
            ingest_key_hash=hash_ingest_key(key),
            created_at=clock.now(),
            budget_usd=payload.budget_usd,
            monthly_spend_usd=0.0,
        )
        db.add(application)
        try:
            db.flush()
            _audit(db, clock, application.id, "application.created", {"name": application.name})
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Application already exists"
            ) from None
        return {**_app_out(application), "ingest_key": key}

    @router.get("/applications", response_model=list[ApplicationOut])
    def list_applications(db: Session = Depends(get_db)) -> list[dict]:
        applications = db.scalars(select(Application).order_by(Application.id)).all()
        return [_app_out(a) for a in applications]

    @router.post("/applications/{application_id}/rotate-key", response_model=RotatedKey)
    def rotate_key(
        application_id: int,
        db: Session = Depends(get_db),
        clock: Clock = Depends(get_clock),
    ) -> dict:
        application = _get_application(db, application_id)
        key = generate_ingest_key()
        application.ingest_key_hash = hash_ingest_key(key)
        _audit(db, clock, application.id, "application.key_rotated", {})
        db.commit()
        return {"id": application.id, "ingest_key": key}

    @router.post(
        "/applications/{application_id}/repositories",
        status_code=status.HTTP_201_CREATED,
        response_model=RepositoryOut,
    )
    def create_repository(
        application_id: int,
        payload: RepositoryCreate,
        db: Session = Depends(get_db),
        clock: Clock = Depends(get_clock),
    ) -> AuthorizedRepository:
        application = _get_application(db, application_id)
        repository = AuthorizedRepository(
            application_id=application.id,
            name=payload.name,
            local_path=payload.local_path,
            clone_url=payload.clone_url,
            base_branch=payload.base_branch,
            test_command=list(payload.test_command),
            github_repo=payload.github_repo,
            created_at=clock.now(),
        )
        db.add(repository)
        try:
            db.flush()
            _audit(
                db,
                clock,
                application.id,
                "repository.registered",
                {"repository_id": repository.id, "name": repository.name},
            )
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Repository already registered"
            ) from None
        return repository

    @router.get(
        "/applications/{application_id}/repositories", response_model=list[RepositoryOut]
    )
    def list_repositories(
        application_id: int, db: Session = Depends(get_db)
    ) -> list[AuthorizedRepository]:
        application = _get_application(db, application_id)
        return list(
            db.scalars(
                select(AuthorizedRepository)
                .where(AuthorizedRepository.application_id == application.id)
                .order_by(AuthorizedRepository.id)
            ).all()
        )

    return router
