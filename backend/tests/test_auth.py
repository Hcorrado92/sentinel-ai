from datetime import datetime, timedelta, timezone
import hashlib

import pytest
from fastapi.testclient import TestClient

from app.clock import FakeClock, SystemClock
from app.config import Settings
from app.main import create_app
from app.security.auth import (
    generate_ingest_key,
    hash_ingest_key,
    verify_ingest_key,
)

ADMIN_TOKEN = "fake-admin-token-for-tests"


@pytest.fixture
def settings(tmp_path, database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        workdir=str(tmp_path / "workdir"),
        admin_token=ADMIN_TOKEN,
    )


def test_generated_keys_are_random_and_long():
    first, second = generate_ingest_key(), generate_ingest_key()
    assert first != second
    assert len(first) >= 32


def test_hash_is_sha256_and_not_plaintext():
    key = generate_ingest_key()
    digest = hash_ingest_key(key)
    assert digest == hashlib.sha256(key.encode()).hexdigest()
    assert key not in digest


def test_verify_ingest_key():
    key = generate_ingest_key()
    stored = hash_ingest_key(key)
    assert verify_ingest_key(key, stored) is True
    assert verify_ingest_key(key + "x", stored) is False
    assert verify_ingest_key("", stored) is False
    assert verify_ingest_key(None, stored) is False
    assert verify_ingest_key(key, None) is False


def test_rotating_key_invalidates_previous_key(client):
    headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
    created = client.post("/api/v1/admin/applications", json={"name": "Rotate App"}, headers=headers)
    assert created.status_code == 201
    app_id = created.json()["id"]
    old_key = created.json()["ingest_key"]

    rotated = client.post(f"/api/v1/admin/applications/{app_id}/rotate-key", headers=headers)
    assert rotated.status_code == 200
    new_key = rotated.json()["ingest_key"]
    assert new_key != old_key

    from app.models import Application

    with client.app.state.session_factory() as session:
        stored_hash = session.get(Application, app_id).ingest_key_hash

    assert verify_ingest_key(old_key, stored_hash) is False
    assert verify_ingest_key(new_key, stored_hash) is True


def test_rotate_key_unknown_application_returns_404(client):
    response = client.post(
        "/api/v1/admin/applications/999/rotate-key",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    "header",
    [None, "Bearer wrong-token", "Basic " + ADMIN_TOKEN, ADMIN_TOKEN],
)
def test_admin_rejects_missing_or_wrong_token(client, header):
    headers = {"Authorization": header} if header is not None else {}
    response = client.get("/api/v1/admin/applications", headers=headers)
    assert response.status_code == 401


def test_admin_returns_503_when_no_token_configured(tmp_path):
    app = create_app(Settings(database_url=f"sqlite:///{(tmp_path / 'noadmin.db').as_posix()}"))
    try:
        with TestClient(app) as client:
            response = client.get(
                "/api/v1/admin/applications", headers={"Authorization": "Bearer anything"}
            )
    finally:
        app.state.engine.dispose()
    assert response.status_code == 503


def test_fake_clock_is_controllable():
    start = datetime(2025, 3, 1, 12, 0, tzinfo=timezone.utc)
    clock = FakeClock(start)
    assert clock.now() == start
    clock.advance(minutes=5)
    assert clock.now() == start + timedelta(minutes=5)
    clock.set(datetime(2025, 4, 1))
    assert clock.now() == datetime(2025, 4, 1, tzinfo=timezone.utc)


def test_system_clock_returns_aware_utc():
    assert SystemClock().now().tzinfo is not None


def test_app_uses_injected_clock(tmp_path):
    clock = FakeClock(datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    settings = Settings(
        database_url=f"sqlite:///{(tmp_path / 'clock.db').as_posix()}",
        admin_token=ADMIN_TOKEN,
    )
    app = create_app(settings, clock=clock)
    try:
        with TestClient(app) as client:
            body = client.post(
                "/api/v1/admin/applications",
                json={"name": "Clock App"},
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            ).json()
    finally:
        app.state.engine.dispose()
    assert body["created_at"].startswith("2025-01-02T03:04:05")
