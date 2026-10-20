from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.ai.base import AIProvider, AIProviderError
from app.ai.mock import MockProvider
from app.clock import FakeClock
from app.config import Settings
from app.jobs.handlers import build_handlers
from app.main import create_app
from app.models import Application, AuditLog, AuthorizedRepository, Event, Incident, Job, RCAReport
from app.rca.schemas import RCAReportModel
from app.worker import run_once

ADMIN_TOKEN = "fake-admin-token-for-tests"
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
START = datetime(2024, 3, 1, 10, tzinfo=timezone.utc)

STACK = (
    "Traceback (most recent call last):\n"
    '  File "/srv/app/demo_app/api.py", line 3, in get_item\n'
    "    return handler(item)\n"
    '  File "/srv/app/demo_app/main.py", line 2, in handler\n'
    "    return item['price'] * 2\n"
    "TypeError: 'NoneType' object is not subscriptable"
)


@pytest.fixture
def settings(tmp_path, database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        workdir=str(tmp_path / "workdir"),
        admin_token=ADMIN_TOKEN,
        job_max_attempts=2,
        job_retry_backoff_seconds=0,
    )


@pytest.fixture
def clock():
    return FakeClock(START)


@pytest.fixture
def app(settings, clock):
    application = create_app(settings, clock=clock)
    yield application
    application.state.engine.dispose()


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def repo_root(tmp_path):
    root = tmp_path / "repo"
    (root / "demo_app").mkdir(parents=True)
    (root / "demo_app" / "main.py").write_bytes(
        b"def handler(item):\n    return item['price'] * 2\n"
    )
    (root / "demo_app" / "api.py").write_bytes(
        b"from demo_app.main import handler\ndef get_item(item):\n    return handler(item)\n"
    )
    return root


@pytest.fixture
def incident_id(app, clock, repo_root) -> int:
    with app.state.session_factory() as db:
        application = Application(name="Demo Shop", slug="demo-shop", ingest_key_hash="0" * 64)
        db.add(application)
        db.flush()
        db.add(
            AuthorizedRepository(
                application_id=application.id,
                name="demo-app",
                local_path=str(repo_root),
                test_command=["python", "-m", "pytest", "-q"],
            )
        )
        incident = Incident(
            application_id=application.id,
            fingerprint="b" * 64,
            title="TypeError: 'NoneType' object is not subscriptable",
            exception_type="TypeError",
            severity="high",
            status="open",
            event_count=1,
            first_seen=clock.now(),
            last_seen=clock.now(),
        )
        db.add(incident)
        db.flush()
        db.add(
            Event(
                application_id=application.id,
                incident_id=incident.id,
                event_id="evt-1",
                timestamp=clock.now(),
                exception_type="TypeError",
                message="'NoneType' object is not subscriptable",
                stack_trace=STACK,
                environment="production",
                metadata_json={"path": "/items/7"},
            )
        )
        db.commit()
        return incident.id


def _process(app, clock, handlers=None):
    return run_once(
        app.state.session_factory,
        handlers or app.state.job_handlers,
        clock=clock,
        backoff_seconds=0,
    )


def _incident_status(app, incident_id) -> str:
    with app.state.session_factory() as db:
        return db.get(Incident, incident_id).status


class ProbeProvider(MockProvider):
    """Mock provider that records the incident status and takes 2 seconds."""

    def __init__(self, app, incident_id, clock):
        super().__init__()
        self.app = app
        self.incident_id = incident_id
        self.clock = clock
        self.seen_statuses: list[str] = []

    def generate_structured(self, prompt, schema):
        self.seen_statuses.append(_incident_status(self.app, self.incident_id))
        self.clock.advance(seconds=2)
        return super().generate_structured(prompt, schema)


class RaisingProvider(AIProvider):
    name = "failing"

    def generate_structured(self, prompt, schema):
        raise AIProviderError("upstream unavailable")


# --- POST /incidents/{id}/rca ---------------------------------------------------


def test_request_rca_requires_admin(client, incident_id):
    assert client.post(f"/api/v1/incidents/{incident_id}/rca").status_code == 401
    assert client.get(f"/api/v1/incidents/{incident_id}/rca").status_code == 401
    assert client.get("/api/v1/runs").status_code == 401


def test_request_rca_unknown_incident_is_404(client):
    assert client.post("/api/v1/incidents/999/rca", headers=AUTH).status_code == 404
    assert client.get("/api/v1/incidents/999/rca", headers=AUTH).status_code == 404


def test_request_rca_returns_202_and_second_request_conflicts(client, app, incident_id):
    response = client.post(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"

    with app.state.session_factory() as db:
        job = db.get(Job, body["job_id"])
        assert job.type == "rca"
        assert job.status == "queued"
        assert job.incident_id == incident_id
        assert job.application_id is not None
        assert job.max_attempts == 2
        actions = [a.action for a in db.query(AuditLog).filter_by(incident_id=incident_id)]
        assert "rca_requested" in actions

    again = client.post(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH)
    assert again.status_code == 409
    assert str(body["job_id"]) in again.json()["detail"]


def test_request_rca_conflicts_while_running(client, app, clock, incident_id):
    from app.jobs import queue

    client.post(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH)
    with app.state.session_factory() as db:
        assert queue.claim_next(db, clock).status == "running"

    assert client.post(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH).status_code == 409


# --- worker + GET /incidents/{id}/rca -------------------------------------------


def test_run_once_processes_rca_job_and_report_is_available(client, app, clock, incident_id):
    assert client.get(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH).status_code == 404
    job_id = client.post(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH).json()["job_id"]

    processed = _process(app, clock)
    assert processed.id == job_id
    assert processed.status == "succeeded"

    response = client.get(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH)
    assert response.status_code == 200
    report = response.json()
    assert report["status"] == "completed"
    assert report["job_id"] == job_id
    assert report["provider"] == "mock"
    assert report["is_mock"] is True
    assert report["cost_usd"] is None
    body = RCAReportModel.model_validate(report["report"])
    assert body.hypotheses
    assert all(h.status == "hypothesis" for h in body.hypotheses)
    # The authorized repository was consulted through the read-only tools.
    assert body.suspect_locations[0].file == "demo_app/main.py"

    with app.state.session_factory() as db:
        job = db.get(Job, job_id)
        assert job.status == "succeeded"
        assert job.result_ref == f"rca_report:{report['id']}"

    # A finished job no longer blocks a new request.
    assert client.post(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH).status_code == 202


def test_incident_is_investigating_during_the_run(client, app, settings, clock, incident_id):
    probe = ProbeProvider(app, incident_id, clock)
    handlers = build_handlers(settings, provider_factory=lambda: probe)
    client.post(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH)

    assert _incident_status(app, incident_id) == "open"
    assert _process(app, clock, handlers).status == "succeeded"
    assert probe.seen_statuses == ["investigating"]
    assert _incident_status(app, incident_id) == "investigating"


def test_failing_rca_is_retried_then_failed_and_status_restored(
    client, app, settings, clock, incident_id
):
    handlers = build_handlers(settings, provider_factory=RaisingProvider)
    job_id = client.post(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH).json()["job_id"]

    first = _process(app, clock, handlers)
    assert first.status == "queued"
    assert first.attempts == 1
    last = _process(app, clock, handlers)
    assert last.id == job_id
    assert last.status == "failed"
    assert last.attempts == 2
    assert "upstream unavailable" in last.error
    assert _incident_status(app, incident_id) == "open"

    with app.state.session_factory() as db:
        reports = db.query(RCAReport).filter_by(incident_id=incident_id).all()
        assert [r.status for r in reports] == ["failed", "failed"]

    latest = client.get(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH).json()
    assert latest["status"] == "failed"
    assert "upstream unavailable" in latest["error"]


def test_inline_mode_processes_job_before_responding(tmp_path, database_url, clock, incident_id, app):
    # Reuse the database seeded through ``app``; build a second app in inline mode.
    inline_settings = Settings(
        database_url=database_url,
        workdir=str(tmp_path / "workdir"),
        admin_token=ADMIN_TOKEN,
        run_jobs_inline=True,
    )
    inline_app = create_app(inline_settings, clock=clock)
    try:
        with TestClient(inline_app) as inline_client:
            response = inline_client.post(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH)
            assert response.status_code == 202
            assert response.json()["status"] == "succeeded"
            report = inline_client.get(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH)
            assert report.status_code == 200
            assert report.json()["status"] == "completed"
    finally:
        inline_app.state.engine.dispose()


def test_run_jobs_inline_defaults_to_false(monkeypatch):
    assert Settings().run_jobs_inline is False
    monkeypatch.setenv("SENTINEL_RUN_JOBS_INLINE", "true")
    assert Settings().run_jobs_inline is True


# --- GET /runs ------------------------------------------------------------------


def test_runs_lists_job_history_with_duration(client, app, settings, clock, incident_id):
    probe = ProbeProvider(app, incident_id, clock)
    handlers = build_handlers(settings, provider_factory=lambda: probe)
    job_id = client.post(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH).json()["job_id"]
    _process(app, clock, handlers)
    clock.advance(minutes=1)
    queued_id = client.post(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH).json()["job_id"]

    response = client.get("/api/v1/runs", headers=AUTH)
    assert response.status_code == 200
    page = response.json()
    assert page["total"] == 2
    newest, done = page["items"]
    assert newest["id"] == queued_id
    assert newest["status"] == "queued"
    assert newest["duration_ms"] is None

    assert done["id"] == job_id
    assert done["type"] == "rca"
    assert done["status"] == "succeeded"
    assert done["duration_ms"] == 2000
    assert done["incident_id"] == incident_id
    assert done["incident_title"].startswith("TypeError")
    assert done["attempts"] == 1
    assert done["result_ref"].startswith("rca_report:")


def test_runs_filters(client, app, clock, incident_id):
    client.post(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH)
    _process(app, clock)
    clock.advance(seconds=1)
    client.post(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH)

    def ids(**params):
        body = client.get("/api/v1/runs", params=params, headers=AUTH).json()
        return [item["status"] for item in body["items"]]

    assert ids(status="succeeded") == ["succeeded"]
    assert ids(type="rca") == ["queued", "succeeded"]
    assert ids(type="repair") == []
    assert ids(incident_id=incident_id + 1) == []
    assert client.get("/api/v1/runs", params={"type": "deploy"}, headers=AUTH).status_code == 422
