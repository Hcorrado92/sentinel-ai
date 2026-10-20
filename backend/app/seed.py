"""Idempotent seed: ``python -m app.seed``.

Creates the ``demo-app`` application and its authorized repository pointing at
this repository's ``demo-app/`` directory. The ingest key is printed only when
the application is created (it is stored only as a hash and cannot be shown
again; rotate it through the admin API if it is lost). Running the command
again leaves existing rows untouched.
"""

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import Clock, SystemClock
from app.config import get_settings
from app.db import create_db_engine, create_session_factory, init_db
from app.models import Application, AuditLog, AuthorizedRepository
from app.security.auth import generate_ingest_key, hash_ingest_key

DEMO_APP_NAME = "demo-app"
DEMO_APP_SLUG = "demo-app"
DEMO_REPOSITORY_NAME = "demo-app"
DEMO_TEST_COMMAND = ["python", "-m", "pytest", "-q", "-m", "not fault_reproduction"]
SEED_ACTOR = "seed"


def default_demo_app_path() -> Path:
    # backend/app/seed.py -> repository root -> demo-app/
    return Path(__file__).resolve().parents[2] / "demo-app"


@dataclass(frozen=True)
class SeedResult:
    application_id: int
    repository_id: int
    application_created: bool
    repository_created: bool
    ingest_key: str | None


def seed(db: Session, *, demo_app_path: Path | None = None, clock: Clock | None = None) -> SeedResult:
    clock = clock or SystemClock()
    path = str((demo_app_path or default_demo_app_path()).resolve())

    ingest_key: str | None = None
    application = db.scalar(
        select(Application).where(
            (Application.slug == DEMO_APP_SLUG) | (Application.name == DEMO_APP_NAME)
        )
    )
    application_created = application is None
    if application is None:
        ingest_key = generate_ingest_key()
        application = Application(
            name=DEMO_APP_NAME,
            slug=DEMO_APP_SLUG,
            ingest_key_hash=hash_ingest_key(ingest_key),
            created_at=clock.now(),
            monthly_spend_usd=0.0,
        )
        db.add(application)
        db.flush()
        db.add(
            AuditLog(
                application_id=application.id,
                actor=SEED_ACTOR,
                action="application.created",
                details={"name": application.name},
                created_at=clock.now(),
            )
        )

    repository = db.scalar(
        select(AuthorizedRepository).where(
            AuthorizedRepository.application_id == application.id,
            AuthorizedRepository.name == DEMO_REPOSITORY_NAME,
        )
    )
    repository_created = repository is None
    if repository is None:
        repository = AuthorizedRepository(
            application_id=application.id,
            name=DEMO_REPOSITORY_NAME,
            local_path=path,
            base_branch="main",
            test_command=list(DEMO_TEST_COMMAND),
            created_at=clock.now(),
        )
        db.add(repository)
        db.flush()
        db.add(
            AuditLog(
                application_id=application.id,
                actor=SEED_ACTOR,
                action="repository.registered",
                details={"repository_id": repository.id, "name": repository.name},
                created_at=clock.now(),
            )
        )

    db.commit()
    return SeedResult(
        application_id=application.id,
        repository_id=repository.id,
        application_created=application_created,
        repository_created=repository_created,
        ingest_key=ingest_key,
    )


def main() -> None:
    settings = get_settings()
    engine = create_db_engine(settings.database_url)
    try:
        init_db(engine)
        with create_session_factory(engine)() as db:
            result = seed(db)
    finally:
        engine.dispose()

    if result.application_created:
        print(f"Created application '{DEMO_APP_NAME}' (id={result.application_id}).")
        print(f"Ingest key (shown only once, store it securely): {result.ingest_key}")
    else:
        print(f"Application '{DEMO_APP_NAME}' already exists (id={result.application_id}).")
    if result.repository_created:
        print(f"Registered authorized repository '{DEMO_REPOSITORY_NAME}' (id={result.repository_id}).")
    else:
        print(f"Authorized repository '{DEMO_REPOSITORY_NAME}' already registered.")


if __name__ == "__main__":
    main()
