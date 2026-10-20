"""Mock provider rules for the demo app's faults, verified against real copies of demo-app/.

Tracebacks come from the real demo app (run in a subprocess inside a tmp copy),
patches are applied to tmp copies and judged by the demo's own test command.
The repository's demo-app/ is never modified.
"""

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ai.base import NoPatchAvailable
from app.ai.mock import MockProvider
from app.ai.mock_rules import DEMO_FAULTS, PatchAnchorNotFound
from app.ai.prompts import build_rca_prompt, build_repair_prompt
from app.clock import FakeClock
from app.config import Settings
from app.db import create_db_engine, create_session_factory, init_db
from app.github.pr import PRNotAllowed, prepare_pull_request, refusal_reason
from app.models import Application, AuthorizedRepository, Event, Incident
from app.rca.engine import gather_context, run_rca
from app.rca.schemas import RCAReportModel
from app.repair.engine import run_repair
from app.repair.patch import FileAction, PatchModel, apply_patch
from app.repo_tools import RepoReader
from app.runner import AllowListRunner, CommandResult, FakeRunner, SubprocessRunner

DEMO_ROOT = Path(__file__).resolve().parents[2] / "demo-app"
TEST_COMMAND = [sys.executable, "-m", "pytest", "-q", "-m", "not fault_reproduction"]
ENV = {"PYTHONDONTWRITEBYTECODE": "1"}
RUNNER = SubprocessRunner(default_timeout=180)
GIT = AllowListRunner(SubprocessRunner(default_timeout=60))
SKIPPED_PARTS = {"__pycache__", ".pytest_cache", ".venv"}
START = datetime(2024, 6, 3, 10, tzinfo=timezone.utc)

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git binary not available")

# fault key -> (exception type, faulty function) in demo_app/main.py
EXPECTED = {
    "null_nickname": ("AttributeError", "_display_name"),
    "quantity_validation": ("ValueError", "_parse_quantity"),
    "discount_calculation": ("DiscountError", "apply_discount"),
}
FAULTS = {fault.key: fault for fault in DEMO_FAULTS}

CAPTURE_SCRIPT = r"""
import json
import traceback

from fastapi.testclient import TestClient

from demo_app.config import DemoSettings
from demo_app.main import create_app


class Capture:
    def __init__(self):
        self.events = []

    def report_exception(self, exc, **kwargs):
        self.events.append(
            {
                "exception_type": type(exc).__name__,
                "message": str(exc),
                "stack_trace": "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                ),
            }
        )


REQUESTS = {
    "null_nickname": ("GET", "/users/2/profile", {}),
    "quantity_validation": (
        "POST", "/orders", {"json": {"product": "widget", "quantity": "three"}}
    ),
    "discount_calculation": ("GET", "/orders/1/total", {"params": {"coupon": "BIGDEAL"}}),
}
out = {}
for key, (method, path, kwargs) in REQUESTS.items():
    capture = Capture()
    app = create_app(DemoSettings(environment="development", faults_enabled=True), reporter=capture)
    client = TestClient(app, raise_server_exceptions=False)
    status = client.request(method, path, **kwargs).status_code
    out[key] = dict(capture.events[0], status=status)
print(json.dumps(out))
"""


def _skipped(path: Path) -> bool:
    return bool(SKIPPED_PARTS.intersection(path.parts)) or path.suffix == ".pyc"


def tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not _skipped(p.relative_to(root))
    }


def copy_demo(dest: Path) -> Path:
    shutil.copytree(
        DEMO_ROOT, dest, ignore=shutil.ignore_patterns(*SKIPPED_PARTS, "*.pyc")
    )
    return dest


def run_tests(root: Path, *extra: str) -> CommandResult:
    return RUNNER.run([*TEST_COMMAND, *extra], cwd=root, timeout=180, env=ENV)


@pytest.fixture(scope="module", autouse=True)
def demo_snapshot():
    """The repository's own demo-app/ must be byte-for-byte unchanged afterwards."""
    before = tree(DEMO_ROOT)
    assert "demo_app/main.py" in before
    yield before
    assert tree(DEMO_ROOT) == before


@pytest.fixture(scope="module")
def traceback_copy(tmp_path_factory) -> Path:
    return copy_demo(tmp_path_factory.mktemp("traceback") / "demo-app")


@pytest.fixture(scope="module")
def tracebacks(traceback_copy) -> dict[str, dict]:
    capture = SubprocessRunner(default_timeout=180, max_output_chars=1_000_000)
    result = capture.run([sys.executable, "-c", CAPTURE_SCRIPT], cwd=traceback_copy, timeout=180, env=ENV)
    assert result.returncode == 0, result.stderr
    captured = json.loads(result.stdout.strip().splitlines()[-1])
    assert set(captured) == set(EXPECTED)
    return captured


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
    return FakeClock(START)


def seed(db, clock, captured: dict, source: Path | None = None):
    application = Application(name="Demo Shop", slug="demo-shop", ingest_key_hash="0" * 64)
    db.add(application)
    db.flush()
    repo = None
    if source is not None:
        repo = AuthorizedRepository(
            application_id=application.id,
            name="demo-app",
            local_path=str(source),
            base_branch="main",
            test_command=TEST_COMMAND,
        )
        db.add(repo)
    incident = Incident(
        application_id=application.id,
        fingerprint="d" * 64,
        title=f"{captured['exception_type']}: {captured['message']}"[:200],
        exception_type=captured["exception_type"],
        severity="high",
        status="open",
        event_count=1,
        first_seen=clock.now(),
        last_seen=clock.now(),
    )
    db.add(incident)
    db.flush()
    event = Event(
        application_id=application.id,
        incident_id=incident.id,
        event_id=f"evt-{incident.id}",
        timestamp=clock.now(),
        exception_type=captured["exception_type"],
        message=captured["message"],
        stack_trace=captured["stack_trace"],
        environment="development",
        metadata_json={},
    )
    db.add(event)
    db.commit()
    return application, repo, incident, event


def mock_rca(captured: dict, root: Path) -> dict:
    """RCA of a synthetic incident through the real prompt builder and the mock."""
    event = SimpleNamespace(
        timestamp=START,
        environment="development",
        exception_type=captured["exception_type"],
        correlation_id=None,
        message=captured["message"],
        metadata_json={},
        stack_trace=captured["stack_trace"],
    )
    excerpts, top = gather_context(RepoReader(root), captured["stack_trace"])
    prompt = build_rca_prompt(incident_stub(captured), [event], excerpts, top)
    return MockProvider().generate_structured(prompt, RCAReportModel).output.model_dump(mode="json")


def incident_stub(captured: dict) -> SimpleNamespace:
    return SimpleNamespace(
        title=f"{captured['exception_type']}: {captured['message']}",
        exception_type=captured["exception_type"],
        severity="high",
        status="open",
        event_count=1,
    )


def mock_patch(captured: dict, rca: dict, root: Path) -> PatchModel:
    prompt = build_repair_prompt(
        incident_stub(captured), rca, [], attempt=1, test_command=TEST_COMMAND, repo_root=str(root)
    )
    return MockProvider().generate_structured(prompt, PatchModel).output


# --- RCA ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_real_traceback_yields_rca_pointing_to_the_faulty_function(
    db, clock, tracebacks, traceback_copy, key
):
    exc_type, function = EXPECTED[key]
    captured = tracebacks[key]
    assert captured["status"] == 500
    assert captured["exception_type"] == exc_type
    _, _, incident, event = seed(db, clock, captured)

    report = run_rca(db, incident, [event], RepoReader(traceback_copy), MockProvider(), clock)

    assert report.status == "completed", report.error
    assert report.provider == "mock"
    body = report.report
    top = body["suspect_locations"][0]
    assert top["file"] == "demo_app/main.py"
    assert top["function"] == function
    assert all(loc["file"] == "demo_app/main.py" for loc in body["suspect_locations"])

    main = body["hypotheses"][0]
    assert main["title"] == FAULTS[key].title
    assert all(0 < h["confidence"] < 1 for h in body["hypotheses"])
    assert all(h["status"] == "hypothesis" for h in body["hypotheses"])
    evidence = "\n".join(main["evidence"])
    assert "Stack frame: demo_app/main.py:" in evidence and f"in {function}" in evidence
    assert "Code excerpt demo_app/main.py:" in evidence
    assert FAULTS[key].code_marker in evidence
    assert body["uncertainties"] and body["next_steps"]
    assert body["summary"].startswith("[mock]") and "unconfirmed" in body["summary"]


def test_unknown_exception_type_falls_back_to_the_generic_rca(tracebacks, traceback_copy):
    captured = dict(tracebacks["null_nickname"], exception_type="KeyError", message="'nickname'")

    rca = mock_rca(captured, traceback_copy)

    assert rca["summary"].startswith("[mock] Likely cause of KeyError")
    assert len(rca["hypotheses"]) == 1
    assert rca["hypotheses"][0]["confidence"] == 0.6


# --- patches -----------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_mock_patch_fixes_a_copy_and_adds_a_failing_regression_test(
    tmp_path, tracebacks, key
):
    fault = FAULTS[key]
    captured = tracebacks[key]
    copy = copy_demo(tmp_path / "patched")
    rca = mock_rca(captured, copy)

    patch = mock_patch(captured, rca, copy)

    changes = {change.path: change for change in patch.files}
    assert changes["demo_app/main.py"].action is FileAction.MODIFY
    assert "Sentinel fix" in changes["demo_app/main.py"].content
    regression = changes[fault.regression_test_path]
    assert regression.action is FileAction.CREATE
    reproduction = changes["tests/test_fault_reproduction.py"]
    for removed in fault.removed_tests:
        assert f"def {removed.name}(" not in reproduction.content
    assert "pytestmark = pytest.mark.fault_reproduction" in reproduction.content
    # Deterministic: the same input gives the same patch.
    assert mock_patch(captured, rca, copy) == patch

    # The regression test fails on an unpatched copy.
    unpatched = copy_demo(tmp_path / "unpatched")
    (unpatched / fault.regression_test_path).write_text(regression.content, encoding="utf-8")
    before = run_tests(unpatched, fault.regression_test_path)
    assert before.returncode == 1, before.stdout + before.stderr
    assert "failed" in before.stdout

    apply_patch(patch, copy, FakeRunner(), test_command=TEST_COMMAND)

    after = run_tests(copy)
    assert after.returncode == 0, after.stdout + after.stderr
    assert "passed" in after.stdout
    assert run_tests(copy, fault.regression_test_path).returncode == 0


def test_missing_anchor_fails_clearly(tmp_path, tracebacks):
    captured = tracebacks["discount_calculation"]
    copy = copy_demo(tmp_path / "demo-app")
    rca = mock_rca(captured, copy)
    main = copy / "demo_app" / "main.py"
    main.write_bytes(main.read_bytes().replace(b"# FAULT 3:", b"# changed:"))

    with pytest.raises(PatchAnchorNotFound, match="anchor not found in demo_app/main.py"):
        mock_patch(captured, rca, copy)


def test_unknown_incident_has_no_patch(tmp_path, tracebacks):
    captured = dict(tracebacks["quantity_validation"], exception_type="KeyError")
    copy = copy_demo(tmp_path / "demo-app")

    with pytest.raises(NoPatchAvailable, match="no patch"):
        mock_patch(captured, mock_rca(captured, copy), copy)


# --- repair engine -----------------------------------------------------------------


def make_settings(tmp_path) -> Settings:
    return Settings(database_url="sqlite://", workdir=str(tmp_path / "workdir"))


@needs_git
@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_repair_engine_verifies_the_mock_fix_on_an_isolated_copy(
    db, clock, tmp_path, tracebacks, key
):
    source = copy_demo(tmp_path / "source")
    original = tree(source)
    _, repo, incident, event = seed(db, clock, tracebacks[key], source)
    provider = MockProvider()
    rca = run_rca(db, incident, [event], RepoReader(source), provider, clock)

    result = run_repair(
        db, incident, rca, repo, provider, GIT, clock, make_settings(tmp_path), test_runner=RUNNER
    )

    assert result.status == "succeeded", [a.error for a in result.attempts]
    assert result.regression_test_failed_before is True
    [attempt] = result.attempts
    assert attempt.tests_passed is True and attempt.returncode == 0
    assert "demo_app/main.py" in attempt.diff
    assert FAULTS[key].regression_test_path in attempt.diff
    assert refusal_reason(attempt, incident) is None
    assert tree(source) == original


def test_unknown_exception_stops_unverifiable_and_no_pr_is_allowed(
    db, clock, tmp_path, tracebacks
):
    source = copy_demo(tmp_path / "source")
    original = tree(source)
    captured = dict(tracebacks["null_nickname"], exception_type="KeyError", message="'nickname'")
    _, repo, incident, event = seed(db, clock, captured, source)
    provider = MockProvider()
    rca = run_rca(db, incident, [event], RepoReader(source), provider, clock)
    assert rca.status == "completed"
    tests = FakeRunner()
    settings = make_settings(tmp_path)

    result = run_repair(db, incident, rca, repo, provider, FakeRunner(), clock, settings, test_runner=tests)

    assert result.status == "stopped"
    assert result.stop_reason == "unverifiable"
    [attempt] = result.attempts
    assert attempt.status == "stopped" and attempt.stop_reason == "unverifiable"
    assert "no_patch_available" in attempt.error
    assert tests.calls == []
    assert refusal_reason(attempt, incident) is not None
    with pytest.raises(PRNotAllowed):
        prepare_pull_request(
            attempt, incident, rca, repo, FakeRunner(), settings, db=db, clock=clock
        )
    assert tree(source) == original


def test_repository_demo_app_is_unchanged(demo_snapshot):
    assert tree(DEMO_ROOT) == demo_snapshot
