from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.ai.base import AIResult, Usage
from app.ai.mock import MockProvider, MockRule
from app.clock import FakeClock
from app.config import Settings
from app.jobs.handlers import build_handlers
from app.main import create_app
from app.models import Application, AuthorizedRepository, Event, Incident, Job, RepairAttempt
from app.repair.confirm import find_confirmed_hypothesis
from app.repair.patch import FileAction, FileChange, PatchModel
from app.runner import CommandResult, FakeRunner
from app.worker import run_once

ADMIN_TOKEN = "fake-admin-token-for-tests"
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
START = datetime(2024, 4, 2, 9, tzinfo=timezone.utc)
TEST_COMMAND = ["python", "-m", "pytest", "-q"]

STACK = (
    "Traceback (most recent call last):\n"
    '  File "/srv/app/demo_app/api.py", line 3, in get_item\n'
    "    return handler(item)\n"
    '  File "/srv/app/demo_app/main.py", line 2, in handler\n'
    "    return item['price'] * 2\n"
    "TypeError: 'NoneType' object is not subscriptable"
)
FIXED = (
    "def handler(item):\n"
    "    if item is None:\n"
    "        return 0\n"
    "    return item['price'] * 2\n"
)
REGRESSION_TEST = (
    "from demo_app.main import handler\n\n\n"
    "def test_handler_accepts_none():\n    assert handler(None) == 0\n"
)
DIFF = (
    "diff --git a/demo_app/main.py b/demo_app/main.py\n"
    "--- a/demo_app/main.py\n"
    "+++ b/demo_app/main.py\n"
    "@@ -1,2 +1,4 @@\n"
    " def handler(item):\n"
    "+    if item is None:\n"
    "+        return 0\n"
    "     return item['price'] * 2\n"
    "diff --git a/tests/test_regression_handler.py b/tests/test_regression_handler.py\n"
    "--- /dev/null\n"
    "+++ b/tests/test_regression_handler.py\n"
    "@@ -0,0 +1,2 @@\n"
    "+def test_handler_accepts_none():\n"
    "+    assert handler(None) == 0\n"
)
SCRIPTED_PATCH = PatchModel(
    files=[
        FileChange(path="demo_app/main.py", action=FileAction.MODIFY, content=FIXED),
        FileChange(
            path="tests/test_regression_handler.py",
            action=FileAction.CREATE,
            content=REGRESSION_TEST,
        ),
    ],
    explanation="Return 0 when the item is missing.",
)


@pytest.fixture
def settings(tmp_path, database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        workdir=str(tmp_path / "workdir"),
        admin_token=ADMIN_TOKEN,
        job_max_attempts=1,
        job_retry_backoff_seconds=0,
        max_repair_attempts=2,
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
    (root / "tests").mkdir()
    (root / "demo_app" / "main.py").write_bytes(
        b"def handler(item):\n    return item['price'] * 2\n"
    )
    (root / "demo_app" / "api.py").write_bytes(
        b"from demo_app.main import handler\ndef get_item(item):\n    return handler(item)\n"
    )
    (root / "tests" / "test_main.py").write_bytes(
        b"from demo_app.main import handler\n\n\ndef test_price():\n"
        b"    assert handler({'price': 2}) == 4\n"
    )
    return root


def _seed_incident(app, clock, slug: str, repo_root=None) -> int:
    with app.state.session_factory() as db:
        application = Application(name=f"App {slug}", slug=slug, ingest_key_hash="0" * 64)
        db.add(application)
        db.flush()
        if repo_root is not None:
            db.add(
                AuthorizedRepository(
                    application_id=application.id,
                    name="demo-app",
                    local_path=str(repo_root),
                    test_command=TEST_COMMAND,
                )
            )
        incident = Incident(
            application_id=application.id,
            fingerprint="c" * 64,
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
                event_id=f"evt-{slug}",
                timestamp=clock.now(),
                exception_type="TypeError",
                message="'NoneType' object is not subscriptable",
                stack_trace=STACK,
                environment="production",
                metadata_json={},
            )
        )
        db.commit()
        return incident.id


@pytest.fixture
def incident_id(app, clock, repo_root) -> int:
    return _seed_incident(app, clock, "demo-shop", repo_root)


def scripted_provider(clock, cost: float | None = None) -> MockProvider:
    """Mock RCA plus a scripted repair patch (the repair takes 3 seconds)."""

    def respond(prompt, schema):
        clock.advance(seconds=3)
        return SCRIPTED_PATCH

    provider = MockProvider(
        rules=[MockRule("scripted-fix", lambda prompt, schema: prompt.task == "repair", respond)]
    )
    if cost is None:
        return provider

    class CostlyProvider(MockProvider):
        name = "fake"
        is_mock = False

        def generate_structured(self, prompt, schema):
            result = provider.generate_structured(prompt, schema)
            if prompt.task != "repair":
                return result
            usage = Usage(input_tokens=120, output_tokens=60, cost_usd=cost)
            return AIResult(output=result.output, usage=usage, provider="fake", model="fake-1")

    return CostlyProvider()


def git_runner() -> FakeRunner:
    return (
        FakeRunner()
        .script(["git", "diff"], CommandResult(returncode=0, stdout=DIFF))
        .script(["git", "rev-parse"], CommandResult(returncode=0, stdout="0123abcd\n"))
    )


def passing_tests() -> FakeRunner:
    # The regression test fails on the pre-patch tree, then the suite passes.
    return FakeRunner(
        results=[
            CommandResult(returncode=1, stdout="E   TypeError\n1 failed in 0.01s"),
            CommandResult(returncode=0, stdout="2 passed in 0.02s"),
        ]
    )


def failing_tests() -> FakeRunner:
    return FakeRunner(
        default=CommandResult(
            returncode=1, stdout="password=fictitious-hunter2\nE   assert 0 == 4\n1 failed in 0.01s"
        )
    )


def handlers_for(settings, clock, tests, cost=None):
    provider = scripted_provider(clock, cost)
    return build_handlers(
        settings, provider_factory=lambda: provider, git_runner=git_runner(), test_runner=tests
    )


def _process(app, clock, handlers):
    return run_once(app.state.session_factory, handlers, clock=clock, backoff_seconds=0)


def _run_rca(client, app, clock, handlers, incident_id):
    assert client.post(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH).status_code == 202
    assert _process(app, clock, handlers).status == "succeeded"


def _repair(client, app, clock, handlers, incident_id) -> Job:
    response = client.post(f"/api/v1/incidents/{incident_id}/repair", headers=AUTH)
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    job = _process(app, clock, handlers)
    assert job.id == response.json()["job_id"]
    assert job.type == "repair"
    return job


def _hypotheses(client, incident_id) -> list[dict]:
    report = client.get(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH).json()
    return report["report"]["hypotheses"]


# --- POST /incidents/{id}/repair -------------------------------------------------


def test_repair_endpoints_require_admin(client, incident_id):
    assert client.post(f"/api/v1/incidents/{incident_id}/repair").status_code == 401
    assert client.get(f"/api/v1/incidents/{incident_id}/repairs").status_code == 401


def test_unknown_incident_is_404(client):
    assert client.post("/api/v1/incidents/999/repair", headers=AUTH).status_code == 404
    assert client.get("/api/v1/incidents/999/repairs", headers=AUTH).status_code == 404


def test_repair_before_any_rca_is_409(client, app, incident_id):
    response = client.post(f"/api/v1/incidents/{incident_id}/repair", headers=AUTH)
    assert response.status_code == 409
    assert "RCA" in response.json()["detail"]
    with app.state.session_factory() as db:
        assert db.query(Job).filter_by(type="repair").count() == 0


def test_repair_without_authorized_repo_is_422(client, app, settings, clock):
    no_repo = _seed_incident(app, clock, "no-repo-shop")
    handlers = handlers_for(settings, clock, passing_tests())
    _run_rca(client, app, clock, handlers, no_repo)

    response = client.post(f"/api/v1/incidents/{no_repo}/repair", headers=AUTH)
    assert response.status_code == 422
    assert "authorized repository" in response.json()["detail"]


def test_second_repair_request_while_active_is_409(client, app, settings, clock, incident_id):
    handlers = handlers_for(settings, clock, passing_tests())
    _run_rca(client, app, clock, handlers, incident_id)

    first = client.post(f"/api/v1/incidents/{incident_id}/repair", headers=AUTH)
    assert first.status_code == 202
    again = client.post(f"/api/v1/incidents/{incident_id}/repair", headers=AUTH)
    assert again.status_code == 409
    assert str(first.json()["job_id"]) in again.json()["detail"]


# --- successful repair -----------------------------------------------------------


def test_successful_repair_proposes_fix_and_confirms_hypothesis(
    client, app, settings, clock, incident_id
):
    handlers = handlers_for(settings, clock, passing_tests())
    _run_rca(client, app, clock, handlers, incident_id)
    assert all(h["status"] == "hypothesis" for h in _hypotheses(client, incident_id))

    job = _repair(client, app, clock, handlers, incident_id)
    assert job.status == "succeeded", job.error

    response = client.get(f"/api/v1/incidents/{incident_id}/repairs", headers=AUTH)
    assert response.status_code == 200
    [attempt] = response.json()
    assert attempt["status"] == "succeeded"
    assert attempt["job_id"] == job.id
    assert attempt["diff"] and "demo_app/main.py" in attempt["diff"]
    assert attempt["test_command"] == TEST_COMMAND
    assert attempt["returncode"] == 0
    assert attempt["tests_passed"] is True
    assert attempt["regression_test_failed_before"] is True
    assert attempt["stop_reason"] is None
    assert attempt["commit_sha"] == "0123abcd"
    assert attempt["branch_name"] == f"sentinel/fix-{incident_id}-1"
    assert "2 passed" in attempt["test_output"]
    assert attempt["duration_ms"] is not None
    assert attempt["cost_usd"] is None
    assert job.result_ref == f"repair_attempt:{attempt['id']}"

    detail = client.get(f"/api/v1/incidents/{incident_id}", headers=AUTH).json()
    assert detail["status"] == "fix_proposed"
    assert detail["latest_repair_status"] == "succeeded"

    hypotheses = _hypotheses(client, incident_id)
    confirmed = [h for h in hypotheses if h["status"] == "confirmed"]
    assert len(confirmed) == 1
    assert confirmed[0]["confirmed_by"]["repair_attempt_id"] == attempt["id"]
    assert confirmed[0]["confirmed_by"]["file"] == "demo_app/main.py"

    timeline = client.get(f"/api/v1/incidents/{incident_id}/timeline", headers=AUTH).json()
    types = [entry["type"] for entry in timeline]
    assert "repair_requested" in types
    assert "repair_succeeded" in types
    assert "rca_hypothesis_confirmed" in types

    # A finished repair job no longer blocks a new request.
    assert client.post(f"/api/v1/incidents/{incident_id}/repair", headers=AUTH).status_code == 202


# --- failed repair ---------------------------------------------------------------


def test_failed_repair_confirms_nothing(client, app, settings, clock, incident_id):
    handlers = handlers_for(settings, clock, failing_tests())
    _run_rca(client, app, clock, handlers, incident_id)

    job = _repair(client, app, clock, handlers, incident_id)
    assert job.status == "failed"
    assert job.attempts == 1
    assert "attempts_exhausted" in job.error

    attempts = client.get(f"/api/v1/incidents/{incident_id}/repairs", headers=AUTH).json()
    assert [a["status"] for a in attempts] == ["failed", "failed"]
    assert attempts[-1]["stop_reason"] == "attempts_exhausted"
    assert all(a["returncode"] == 1 for a in attempts)
    assert all("fictitious-hunter2" not in (a["test_output"] or "") for a in attempts)

    assert all(h["status"] == "hypothesis" for h in _hypotheses(client, incident_id))
    detail = client.get(f"/api/v1/incidents/{incident_id}", headers=AUTH).json()
    assert detail["status"] != "fix_proposed"
    assert detail["latest_repair_status"] == "failed"
    with app.state.session_factory() as db:
        assert db.query(RepairAttempt).filter_by(status="succeeded").count() == 0


def test_unrelated_patch_confirms_no_hypothesis():
    report = {
        "hypotheses": [
            {"title": "Bug in handler", "description": "", "confidence": 0.6, "evidence": ["x"]}
        ],
        "suspect_locations": [{"file": "demo_app/main.py", "function": "handler", "reason": "top"}],
    }
    unrelated = RepairAttempt(
        status="succeeded",
        tests_passed=True,
        diff="--- a/other.py\n+++ b/other.py\n@@ -1 +1 @@\n-a\n+b\n",
    )
    assert find_confirmed_hypothesis(report, unrelated) is None
    related = RepairAttempt(status="succeeded", tests_passed=True, diff=DIFF)
    assert find_confirmed_hypothesis(report, related) == (0, "demo_app/main.py")


# --- GET /runs and costs ---------------------------------------------------------


def test_runs_include_repair_run_with_duration_and_null_mock_cost(
    client, app, settings, clock, incident_id
):
    handlers = handlers_for(settings, clock, passing_tests())
    _run_rca(client, app, clock, handlers, incident_id)
    job = _repair(client, app, clock, handlers, incident_id)

    page = client.get("/api/v1/runs", params={"type": "repair"}, headers=AUTH).json()
    [run] = page["items"]
    assert run["id"] == job.id
    assert run["type"] == "repair"
    assert run["status"] == "succeeded"
    assert run["result"] == "succeeded"
    assert run["stop_reason"] is None
    assert run["duration_ms"] == 3000
    assert run["incident_id"] == incident_id
    assert run["cost_usd"] is None
    assert run["input_tokens"] is None and run["output_tokens"] is None

    all_runs = client.get("/api/v1/runs", headers=AUTH).json()
    assert {r["type"] for r in all_runs["items"]} == {"rca", "repair"}
    rca_run = next(r for r in all_runs["items"] if r["type"] == "rca")
    assert rca_run["result"] == "completed"
    assert rca_run["cost_usd"] is None

    [application] = client.get("/api/v1/applications", headers=AUTH).json()
    assert application["ai_cost_usd"] is None


def test_failed_repair_run_records_its_result(client, app, settings, clock, incident_id):
    handlers = handlers_for(settings, clock, failing_tests())
    _run_rca(client, app, clock, handlers, incident_id)
    _repair(client, app, clock, handlers, incident_id)

    [run] = client.get("/api/v1/runs", params={"type": "repair"}, headers=AUTH).json()["items"]
    assert run["status"] == "failed"
    assert run["result"] == "failed"
    assert run["stop_reason"] == "attempts_exhausted"
    assert run["duration_ms"] == 6000


def test_reported_costs_are_aggregated_per_run_and_application(
    client, app, settings, clock, incident_id
):
    handlers = handlers_for(settings, clock, passing_tests(), cost=0.25)
    _run_rca(client, app, clock, handlers, incident_id)
    _repair(client, app, clock, handlers, incident_id)

    [run] = client.get("/api/v1/runs", params={"type": "repair"}, headers=AUTH).json()["items"]
    assert run["cost_usd"] == pytest.approx(0.25)
    assert run["input_tokens"] == 120 and run["output_tokens"] == 60

    [application] = client.get("/api/v1/applications", headers=AUTH).json()
    assert application["ai_cost_usd"] == pytest.approx(0.25)
