from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.ai.base import AIProvider, AIProviderError, AIResult, Usage
from app.ai.mock import MockProvider, MockRule
from app.clock import FakeClock
from app.db import create_db_engine, create_session_factory, init_db
from app.models import Application, AuditLog, Event, Incident, RCAReport
from app.rca.engine import gather_context, parse_frames, run_rca
from app.rca.schemas import RCAReportModel, confirm_hypothesis
from app.repo_tools import RepoReader

STACK = (
    "Traceback (most recent call last):\n"
    '  File "/srv/app/.venv/lib/python3.12/site-packages/starlette/routing.py", line 74, in app\n'
    "    response = await func(request)\n"
    '  File "/srv/app/demo_app/api.py", line 3, in get_item\n'
    "    return handler(item)\n"
    '  File "/srv/app/demo_app/main.py", line 2, in handler\n'
    "    return item['price'] * 2\n"
    "TypeError: 'NoneType' object is not subscriptable"
)


@pytest.fixture
def db(database_url):
    engine = create_db_engine(database_url)
    init_db(engine)
    session = create_session_factory(engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def clock():
    return FakeClock(datetime(2024, 3, 1, 10, tzinfo=timezone.utc))


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "demo_app").mkdir(parents=True)
    (root / "demo_app" / "main.py").write_bytes(
        b"def handler(item):\n    return item['price'] * 2\n"
    )
    (root / "demo_app" / "api.py").write_bytes(
        b"from demo_app.main import handler\ndef get_item(item):\n    return handler(item)\n"
    )
    return RepoReader(root)


@pytest.fixture
def incident(db, clock):
    application = Application(name="Demo Shop", slug="demo-shop", ingest_key_hash="0" * 64)
    db.add(application)
    db.flush()
    incident = Incident(
        application_id=application.id,
        fingerprint="a" * 64,
        title="TypeError: 'NoneType' object is not subscriptable",
        exception_type="TypeError",
        severity="high",
        status="open",
        event_count=2,
        first_seen=clock.now(),
        last_seen=clock.now(),
    )
    db.add(incident)
    db.flush()
    for n in range(2):
        db.add(
            Event(
                application_id=application.id,
                incident_id=incident.id,
                event_id=f"evt-{n}",
                timestamp=clock.now() + timedelta(minutes=n),
                exception_type="TypeError",
                message="'NoneType' object is not subscriptable",
                stack_trace=STACK,
                environment="production",
                metadata_json={"path": "/items/7"},
            )
        )
    db.commit()
    return incident


def _events(db, incident):
    return list(db.scalars(select(Event).where(Event.incident_id == incident.id)))


def _actions(db, incident):
    return [
        entry.action
        for entry in db.scalars(
            select(AuditLog)
            .where(AuditLog.incident_id == incident.id)
            .order_by(AuditLog.id)
        )
    ]


class RaisingProvider(AIProvider):
    name = "failing"

    def generate_structured(self, prompt, schema):
        raise AIProviderError("upstream unavailable api_key=sk-fictitious0123456789abcdef")


class ConfirmingProvider(AIProvider):
    """Returns an output that bypassed validation and claims a confirmed cause."""

    name = "confirming"

    def generate_structured(self, prompt, schema):
        report = RCAReportModel.model_validate(
            {
                "summary": "Null item",
                "hypotheses": [
                    {
                        "title": "item is None",
                        "description": "handler receives None",
                        "confidence": 0.9,
                        "evidence": ["Top frame demo_app/main.py:2"],
                    }
                ],
                "suspect_locations": [
                    {"file": "demo_app/main.py", "function": "handler", "reason": "top frame"}
                ],
            }
        )
        forged = confirm_hypothesis(report, 0)
        assert forged.hypotheses[0].status == "confirmed"
        return AIResult(
            output=forged,
            usage=Usage(input_tokens=100, output_tokens=50, cost_usd=0.0012),
            provider=self.name,
            model="fake-model",
        )


class SlowMockProvider(MockProvider):
    def __init__(self, clock):
        super().__init__()
        self.clock = clock

    def generate_structured(self, prompt, schema):
        self.clock.advance(seconds=1.5)
        return super().generate_structured(prompt, schema)


# --- engine -------------------------------------------------------------------


def test_mock_rca_persists_evidence_based_report(db, incident, repo, clock):
    report = run_rca(db, incident, _events(db, incident), repo, MockProvider(), clock)

    stored = db.get(RCAReport, report.id)
    assert stored.status == "completed"
    assert stored.provider == "mock"
    assert stored.error is None
    assert stored.input_tokens is None and stored.cost_usd is None
    body = RCAReportModel.model_validate(stored.report)
    assert body.hypotheses
    for hypothesis in body.hypotheses:
        assert hypothesis.evidence
        assert hypothesis.status == "hypothesis"
        assert 0 < hypothesis.confidence < 1
    files = [location.file for location in body.suspect_locations]
    assert files[0] == "demo_app/main.py"
    assert body.suspect_locations[0].function == "handler"
    assert "demo_app/api.py" in files
    assert stored.summary == body.summary
    assert body.uncertainties
    assert _actions(db, incident)[-2:] == ["rca_started", "rca_completed"]


def test_duration_is_measured_with_the_clock(db, incident, repo, clock):
    report = run_rca(db, incident, _events(db, incident), repo, SlowMockProvider(clock), clock)

    assert report.status == "completed"
    assert report.duration_ms == 1500


def test_confirmed_status_from_model_is_stored_as_hypothesis(db, incident, repo, clock):
    report = run_rca(db, incident, _events(db, incident), repo, ConfirmingProvider(), clock)

    stored = db.get(RCAReport, report.id)
    assert stored.status == "completed"
    assert [h["status"] for h in stored.report["hypotheses"]] == ["hypothesis"]
    assert stored.input_tokens == 100
    assert stored.output_tokens == 50
    assert stored.cost_usd == pytest.approx(0.0012)
    assert stored.model == "fake-model"


def test_confirmed_status_from_mock_rule_is_downgraded(db, incident, repo, clock):
    rule = MockRule(
        name="confirmed",
        matches=lambda prompt, schema: prompt.task == "rca",
        respond=lambda prompt, schema: {
            "summary": "Confirmed!",
            "hypotheses": [
                {
                    "title": "t",
                    "description": "d",
                    "confidence": 0.5,
                    "evidence": ["e"],
                    "status": "confirmed",
                }
            ],
            "suspect_locations": [],
            "uncertainties": [],
            "next_steps": [],
        },
    )
    report = run_rca(db, incident, _events(db, incident), repo, MockProvider([rule]), clock)

    assert report.report["hypotheses"][0]["status"] == "hypothesis"


def test_provider_error_produces_failed_report(db, incident, repo, clock):
    report = run_rca(db, incident, _events(db, incident), repo, RaisingProvider(), clock)

    stored = db.get(RCAReport, report.id)
    assert stored.status == "failed"
    assert "upstream unavailable" in stored.error
    assert "AIProviderError" in stored.error
    assert "sk-fictitious0123456789abcdef" not in stored.error
    assert stored.provider == "failing"
    assert stored.finished_at is not None
    assert _actions(db, incident)[-2:] == ["rca_started", "rca_failed"]
    failed = db.scalars(
        select(AuditLog).where(AuditLog.action == "rca_failed")
    ).one()
    assert failed.details["rca_report_id"] == stored.id
    assert "upstream unavailable" in failed.details["error"]


def test_invalid_model_output_produces_failed_report(db, incident, repo, clock):
    rule = MockRule("bad", lambda p, s: True, lambda p, s: {"summary": "x", "hypotheses": "no"})
    report = run_rca(db, incident, _events(db, incident), repo, MockProvider([rule]), clock)

    assert report.status == "failed"
    assert "StructuredOutputError" in report.error


def test_without_repository_rca_still_completes(db, incident, clock):
    report = run_rca(db, incident, _events(db, incident), None, MockProvider(), clock)

    assert report.status == "completed"
    assert report.report["suspect_locations"] == []
    assert report.report["hypotheses"][0]["confidence"] < 1


def test_rca_entries_appear_in_timeline(db, incident, repo, clock):
    from app.api.dashboard import TIMELINE_PREFIXES

    run_rca(db, incident, _events(db, incident), repo, MockProvider(), clock)

    assert all(a.startswith(TIMELINE_PREFIXES) for a in _actions(db, incident))


# --- context gathering --------------------------------------------------------


def test_parse_frames_innermost_first():
    frames = parse_frames(STACK)

    assert frames[0].path == "/srv/app/demo_app/main.py"
    assert frames[0].function == "handler"
    assert frames[0].line == 2


def test_gather_context_maps_top_frame_and_skips_site_packages(repo):
    excerpts, top = gather_context(repo, STACK)

    assert top == {"file": "demo_app/main.py", "function": "handler", "line": 2}
    assert [e.path for e in excerpts] == ["demo_app/main.py", "demo_app/api.py"]
    assert "item['price']" in excerpts[0].content


# --- schema -------------------------------------------------------------------


def test_schema_downgrades_confirmed_and_validates_ranges():
    valid = {
        "summary": "s",
        "hypotheses": [
            {"title": "t", "description": "d", "confidence": 0.4, "evidence": ["e"], "status": "confirmed"}
        ],
    }
    model = RCAReportModel.model_validate(valid)
    assert model.hypotheses[0].status == "hypothesis"
    assert model.suspect_locations == [] and model.next_steps == []

    for bad in (
        {**valid["hypotheses"][0], "confidence": 1.5},
        {**valid["hypotheses"][0], "evidence": []},
    ):
        with pytest.raises(ValidationError):
            RCAReportModel.model_validate({"summary": "s", "hypotheses": [bad]})
