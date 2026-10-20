from pathlib import Path

from sqlalchemy import func, select

from app.db import create_db_engine, create_session_factory, init_db
from app.models import Application, AuthorizedRepository
from app.security.auth import verify_ingest_key
from app.seed import DEMO_TEST_COMMAND, default_demo_app_path, main, seed


def _session_factory(database_url: str):
    engine = create_db_engine(database_url)
    init_db(engine)
    return engine, create_session_factory(engine)


def test_seed_creates_demo_app_and_repository(database_url, tmp_path):
    engine, factory = _session_factory(database_url)
    demo_path = tmp_path / "demo-app"
    try:
        with factory() as db:
            result = seed(db, demo_app_path=demo_path)
        assert result.application_created and result.repository_created
        assert result.ingest_key is not None

        with factory() as db:
            application = db.scalars(select(Application)).one()
            assert application.name == "demo-app"
            assert application.slug == "demo-app"
            assert verify_ingest_key(result.ingest_key, application.ingest_key_hash)
            assert application.ingest_key_hash != result.ingest_key
            repository = db.scalars(select(AuthorizedRepository)).one()
            assert repository.application_id == application.id
            assert repository.test_command == DEMO_TEST_COMMAND
            assert repository.test_command == [
                "python", "-m", "pytest", "-q", "-m", "not fault_reproduction"
            ]
            assert Path(repository.local_path) == demo_path.resolve()
    finally:
        engine.dispose()


def test_seed_twice_is_idempotent(database_url, tmp_path):
    engine, factory = _session_factory(database_url)
    try:
        with factory() as db:
            first = seed(db, demo_app_path=tmp_path / "demo-app")
        with factory() as db:
            second = seed(db, demo_app_path=tmp_path / "demo-app")

        assert second.application_created is False
        assert second.repository_created is False
        assert second.ingest_key is None
        assert second.application_id == first.application_id
        assert second.repository_id == first.repository_id

        with factory() as db:
            assert db.scalar(
                select(func.count()).select_from(Application).where(Application.name == "demo-app")
            ) == 1
            assert db.scalar(select(func.count()).select_from(AuthorizedRepository)) == 1
            application = db.get(Application, first.application_id)
            # The original key keeps working: the second run did not rotate it.
            assert verify_ingest_key(first.ingest_key, application.ingest_key_hash)
    finally:
        engine.dispose()


def test_default_demo_app_path_points_at_repo_demo_app():
    path = default_demo_app_path()
    assert path.name == "demo-app"
    assert (path.parent / "backend" / "app" / "seed.py").is_file()


def test_main_prints_key_only_on_creation(database_url, monkeypatch, capsys):
    monkeypatch.setenv("SENTINEL_DATABASE_URL", database_url)
    main()
    first = capsys.readouterr().out
    assert "Ingest key" in first
    assert "sk_sentinel_" in first

    main()
    second = capsys.readouterr().out
    assert "already exists" in second
    assert "sk_sentinel_" not in second

    engine, factory = _session_factory(database_url)
    try:
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(Application)) == 1
            assert db.scalar(select(func.count()).select_from(AuthorizedRepository)) == 1
    finally:
        engine.dispose()
