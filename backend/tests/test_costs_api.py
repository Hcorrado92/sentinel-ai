from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.clock import FakeClock
from app.config import Settings
from app.main import create_app
from app.models import Application, AuditLog, Event, Incident, Job, RCAReport, RepairAttempt
from app.worker import run_once

ADMIN_TOKEN = "fake-admin-token-for-tests"
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
START = datetime(2024, 3, 1, 10, tzinfo=timezone.utc)


@pytest.fixture
def settings(tmp_path, database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        workdir=str(tmp_path / "workdir"),
        admin_token=ADMIN_TOKEN,
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


def _application(db, name: str) -> Application:
    application = Application(name=name, slug=name.lower().replace(" ", "-"), ingest_key_hash="0" * 64)
    db.add(application)
    db.flush()
    return application


def _incident(db, application: Application, fingerprint: str) -> Incident:
    incident = Incident(
        application_id=application.id,
        fingerprint=fingerprint * 64,
        title="ValueError: invalid quantity",
        exception_type="ValueError",
        severity="medium",
        status="open",
        event_count=1,
        first_seen=START,
        last_seen=START,
    )
    db.add(incident)
    db.flush()
    return incident


def test_costs_requires_admin(client):
    assert client.get("/api/v1/costs").status_code == 401


def test_costs_empty_without_applications(client):
    response = client.get("/api/v1/costs", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "input_tokens": None,
        "output_tokens": None,
        "cost_usd": None,
    }


def test_costs_are_null_when_only_mock_runs_exist(client, app):
    with app.state.session_factory() as db:
        application = _application(db, "Mock Shop")
        incident = _incident(db, application, "a")
        db.add(RCAReport(incident_id=incident.id, status="completed", provider="mock"))
        db.add(RepairAttempt(incident_id=incident.id, status="succeeded"))
        db.add(RepairAttempt(incident_id=incident.id, attempt_number=2, status="failed"))
        db.commit()

    body = client.get("/api/v1/costs", headers=AUTH).json()
    assert body["cost_usd"] is None
    assert body["input_tokens"] is None
    [item] = body["items"]
    assert item["application_name"] == "Mock Shop"
    assert item["rca_runs"] == 1
    assert item["repair_attempts"] == 2
    assert item["runs_without_cost"] == 3
    assert item["input_tokens"] is None
    assert item["output_tokens"] is None
    assert item["cost_usd"] is None


def test_costs_returns_per_application_totals(client, app):
    with app.state.session_factory() as db:
        paid = _application(db, "Paid Shop")
        mock = _application(db, "Mock Shop")
        idle = _application(db, "Idle Shop")
        first = _incident(db, paid, "b")
        second = _incident(db, paid, "c")
        db.add(
            RCAReport(
                incident_id=first.id,
                status="completed",
                provider="anthropic",
                input_tokens=1000,
                output_tokens=200,
                cost_usd=0.25,
            )
        )
        db.add(
            RepairAttempt(
                incident_id=second.id,
                status="succeeded",
                input_tokens=500,
                output_tokens=100,
                cost_usd=0.125,
            )
        )
        # A run without a reported cost does not change the totals.
        db.add(RepairAttempt(incident_id=second.id, attempt_number=2, status="failed"))
        db.add(RCAReport(incident_id=_incident(db, mock, "d").id, status="completed", provider="mock"))
        db.commit()
        paid_id, mock_id, idle_id = paid.id, mock.id, idle.id

    body = client.get("/api/v1/costs", headers=AUTH).json()
    items = {item["application_id"]: item for item in body["items"]}
    assert set(items) == {paid_id, mock_id, idle_id}

    assert items[paid_id]["rca_runs"] == 1
    assert items[paid_id]["repair_attempts"] == 2
    assert items[paid_id]["runs_without_cost"] == 1
    assert items[paid_id]["input_tokens"] == 1500
    assert items[paid_id]["output_tokens"] == 300
    assert items[paid_id]["cost_usd"] == pytest.approx(0.375)

    assert items[mock_id]["rca_runs"] == 1
    assert items[mock_id]["cost_usd"] is None
    assert items[idle_id]["rca_runs"] == 0
    assert items[idle_id]["cost_usd"] is None

    assert body["input_tokens"] == 1500
    assert body["cost_usd"] == pytest.approx(0.375)


def test_runs_report_the_provider(client, app, clock):
    with app.state.session_factory() as db:
        application = _application(db, "Demo Shop")
        incident = _incident(db, application, "e")
        db.add(
            Event(
                application_id=application.id,
                incident_id=incident.id,
                event_id="evt-1",
                timestamp=START,
                exception_type="ValueError",
                message="invalid quantity",
                stack_trace="ValueError: invalid quantity",
                environment="production",
            )
        )
        repair_job = Job(
            type="repair",
            incident_id=incident.id,
            application_id=application.id,
            status="failed",
            created_at=START,
        )
        db.add(repair_job)
        db.flush()
        db.add(
            AuditLog(
                incident_id=incident.id,
                application_id=application.id,
                actor="sentinel-repair",
                action="repair_started",
                details={"job_id": repair_job.id, "provider": "mock"},
            )
        )
        db.commit()
        incident_id, repair_job_id = incident.id, repair_job.id

    clock.advance(minutes=1)
    rca_job_id = client.post(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH).json()["job_id"]
    run_once(app.state.session_factory, app.state.job_handlers, clock=clock, backoff_seconds=0)

    items = {item["id"]: item for item in client.get("/api/v1/runs", headers=AUTH).json()["items"]}
    assert items[rca_job_id]["provider"] == "mock"
    assert items[rca_job_id]["cost_usd"] is None
    assert items[repair_job_id]["provider"] == "mock"
