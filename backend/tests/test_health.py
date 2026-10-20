from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from app.config import Settings
from app.db import Base, get_db
from app.main import create_app


def test_health_returns_ok_with_mock_provider(client):
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    providers = body.pop("providers")
    assert body == {
        "status": "ok",
        "ai_provider": "mock",
        "ai_provider_mock": True,
    }
    assert [p["name"] for p in providers] == ["mock", "anthropic", "openai", "gemini"]


def test_health_reports_non_mock_provider(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'other.db').as_posix()}",
        ai_provider="anthropic",
    )
    app = create_app(settings)
    try:
        with TestClient(app) as client:
            body = client.get("/api/v1/health").json()
    finally:
        app.state.engine.dispose()

    assert body["ai_provider"] == "anthropic"
    assert body["ai_provider_mock"] is False


def test_create_app_creates_tables_in_tmp_path_only(tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    db_file = tmp_path / "isolated.db"

    app = create_app(Settings(database_url=f"sqlite:///{db_file.as_posix()}"))
    try:
        existing = set(inspect(app.state.engine).get_table_names())
    finally:
        app.state.engine.dispose()

    assert db_file.exists()
    assert set(Base.metadata.tables).issubset(existing)
    assert list(cwd.iterdir()) == []


def test_get_db_yields_working_session(app):
    gen = get_db(SimpleNamespace(app=app))
    session = next(gen)
    try:
        assert session.execute(text("SELECT 1")).scalar() == 1
    finally:
        gen.close()
