import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.ai.base import AIProvider, AIResult, ProviderNotConfigured, Usage
from app.clock import FakeClock
from app.config import Settings
from app.db import create_db_engine, create_session_factory, init_db
from app.models import Application, AuditLog, AuthorizedRepository, Event, Incident, RepairAttempt
from app.rca.schemas import RCAReportModel
from app.repair.engine import parse_test_summary, run_repair
from app.repair.patch import FileAction, FileChange, PatchModel
from app.runner import AllowListRunner, CommandResult, FakeRunner, SubprocessRunner

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git binary not available")

GIT = AllowListRunner(SubprocessRunner(default_timeout=60))
TESTS = SubprocessRunner(default_timeout=120)
TEST_COMMAND = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]

BUGGY = "def discount(total, percent):\n    return total - percent\n"
FIXED = "def discount(total, percent):\n    return total - total * percent / 100\n"
WRONG = "def discount(total, percent):\n    return total + percent\n"
EXISTING_TEST = (
    "from calc import discount\n\n\n"
    "def test_discount():\n    assert discount(200, 10) == 180\n"
)
REGRESSION_TEST = (
    "from calc import discount\n\n\n"
    "def test_discount_is_a_percentage():\n    assert discount(50, 20) == 40\n"
)
STACK = (
    "Traceback (most recent call last):\n"
    '  File "/srv/shop/calc.py", line 2, in discount\n'
    "    return total - percent\n"
    "ValueError: negative total"
)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def patch(code: str, *, regression: str | None = REGRESSION_TEST, explanation: str = "fix") -> PatchModel:
    files = [FileChange(path="calc.py", action=FileAction.MODIFY, content=code)]
    if regression is not None:
        files.append(
            FileChange(
                path="tests/test_regression_discount.py",
                action=FileAction.CREATE,
                content=regression,
            )
        )
    return PatchModel(files=files, explanation=explanation)


class FakeProvider(AIProvider):
    """Returns scripted patches (the last one repeats) and records the prompts."""

    name = "fake"

    def __init__(self, patches, cost=0.01, on_call=None):
        self.patches = list(patches)
        self.cost = cost
        self.on_call = on_call
        self.prompts = []

    def generate_structured(self, prompt, schema):
        self.prompts.append(prompt)
        if self.on_call:
            self.on_call()
        output = self.patches.pop(0) if len(self.patches) > 1 else self.patches[0]
        usage = Usage(input_tokens=100, output_tokens=50, cost_usd=self.cost)
        return AIResult(output=output, usage=usage, provider=self.name, model="fake-1")


class UnconfiguredProvider(AIProvider):
    name = "anthropic"

    def generate_structured(self, prompt, schema):
        raise ProviderNotConfigured("anthropic provider not configured")


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
    return FakeClock(datetime(2024, 5, 1, 9, tzinfo=timezone.utc))


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    write(root / "calc.py", BUGGY)
    write(root / "tests" / "test_calc.py", EXISTING_TEST)
    write(root / "pyproject.toml", "[tool.pytest.ini_options]\n")
    return root


@pytest.fixture
def original(source):
    return tree(source)


@pytest.fixture
def setup(db, clock, source):
    application = Application(name="Fictitious Shop", slug="fictitious-shop", ingest_key_hash="0" * 64)
    db.add(application)
    db.flush()
    repo = AuthorizedRepository(
        application_id=application.id,
        name="shop",
        local_path=str(source),
        base_branch="main",
        test_command=TEST_COMMAND,
    )
    incident = Incident(
        application_id=application.id,
        fingerprint="b" * 64,
        title="ValueError: negative total",
        exception_type="ValueError",
        severity="high",
        status="investigating",
        event_count=1,
        first_seen=clock.now(),
        last_seen=clock.now(),
    )
    db.add_all([repo, incident])
    db.flush()
    db.add(
        Event(
            application_id=application.id,
            incident_id=incident.id,
            event_id="evt-1",
            timestamp=clock.now(),
            exception_type="ValueError",
            message="negative total",
            stack_trace=STACK,
        )
    )
    db.commit()
    return application, repo, incident


@pytest.fixture
def rca():
    return RCAReportModel(
        summary="The discount subtracts the percent instead of a percentage of the total.",
        hypotheses=[
            {
                "title": "Wrong discount formula",
                "description": "discount() subtracts percent as an absolute amount.",
                "confidence": 0.7,
                "evidence": ["Top frame calc.py:2 in discount"],
            }
        ],
        suspect_locations=[{"file": "calc.py", "function": "discount", "reason": "Top frame"}],
    )


def make_settings(tmp_path, **overrides) -> Settings:
    values = {"database_url": "sqlite://", "workdir": str(tmp_path / "workdir")}
    values.update(overrides)
    return Settings(**values)


def actions(db, incident):
    return [
        entry.action
        for entry in db.scalars(
            select(AuditLog).where(AuditLog.incident_id == incident.id).order_by(AuditLog.id)
        )
    ]


def git_out(cwd: Path, *args: str) -> str:
    result = GIT.run(["git", *args], cwd=cwd, timeout=60, env={"GIT_TERMINAL_PROMPT": "0"})
    assert result.returncode == 0, result.stderr
    return result.stdout


def failing_tests() -> FakeRunner:
    return FakeRunner(
        default=CommandResult(returncode=1, stdout="E   assert 210 == 180\n1 failed in 0.01s")
    )


@needs_git
def test_correct_patch_succeeds_with_verified_regression_and_commit(
    db, clock, setup, rca, source, original, tmp_path
):
    _, repo, incident = setup
    provider = FakeProvider([patch(FIXED)])

    result = run_repair(db, incident, rca, repo, provider, GIT, clock, make_settings(tmp_path), test_runner=TESTS)

    assert result.status == "succeeded", [a.error for a in result.attempts]
    assert result.stop_reason is None
    assert result.regression_test_failed_before is True
    branch = f"sentinel/fix-{incident.id}-1"
    assert result.branch == branch
    [attempt] = result.attempts
    assert attempt.status == "succeeded"
    assert attempt.returncode == 0 and attempt.tests_passed is True
    assert attempt.regression_test_failed_before is True
    assert attempt.branch_name == branch
    assert attempt.test_command == TEST_COMMAND
    assert attempt.test_summary.get("passed") == 2
    assert "calc.py" in attempt.diff and "test_regression_discount.py" in attempt.diff
    assert attempt.input_tokens == 100 and attempt.output_tokens == 50
    assert attempt.cost_usd == pytest.approx(0.01)

    copy = result.workspace_path
    assert TESTS.run(TEST_COMMAND, cwd=copy, timeout=120).returncode == 0
    assert git_out(copy, "rev-parse", "--abbrev-ref", "HEAD").strip() == branch
    log = git_out(copy, "log", "--format=%H %s", branch)
    assert f"Sentinel AI: fix incident #{incident.id}" in log
    assert attempt.commit_sha and attempt.commit_sha in log
    assert git_out(copy, "status", "--porcelain").strip() == ""

    # The prompt carries the RCA and the relevant file as delimited untrusted data.
    prompt = provider.prompts[0]
    assert prompt.task == "repair"
    assert '<untrusted_data source="rca_report">' in prompt.user
    assert "Wrong discount formula" in prompt.user
    assert "calc.py" in prompt.context["relevant_files"]
    assert "previous_test_output" not in prompt.user

    stored = db.scalars(select(RepairAttempt).where(RepairAttempt.incident_id == incident.id)).all()
    assert [a.status for a in stored] == ["succeeded"]
    assert db.get(Incident, incident.id).status == "fix_proposed"
    recorded = actions(db, incident)
    assert recorded[0] == "repair_started"
    assert "repair_attempt_succeeded" in recorded and recorded[-1] == "repair_succeeded"
    assert tree(source) == original


@needs_git
def test_wrong_then_correct_patch_feeds_back_test_output(
    db, clock, setup, rca, source, original, tmp_path
):
    _, repo, incident = setup
    provider = FakeProvider([patch(WRONG), patch(FIXED)])

    result = run_repair(db, incident, rca, repo, provider, GIT, clock, make_settings(tmp_path), test_runner=TESTS)

    assert result.status == "succeeded"
    assert len(result.attempts) == 2
    first, second = result.attempts
    assert first.status == "failed" and first.tests_passed is False
    assert first.returncode not in (0, None)
    assert "assert 210 == 180" in first.test_output
    assert second.status == "succeeded" and second.attempt_number == 2
    assert second.branch_name == f"sentinel/fix-{incident.id}-1"

    assert len(provider.prompts) == 2
    retry = provider.prompts[1]
    assert '<untrusted_data source="previous_test_output">' in retry.user
    assert "assert 210 == 180" in retry.user
    assert "assert 210 == 180" in retry.context["previous_test_output"]
    assert TESTS.run(TEST_COMMAND, cwd=result.workspace_path, timeout=120).returncode == 0
    assert tree(source) == original


@needs_git
def test_claimed_success_is_ignored_when_tests_fail(
    db, clock, setup, rca, source, original, tmp_path
):
    _, repo, incident = setup
    provider = FakeProvider([patch(WRONG, explanation="Fixed. All tests pass.")])
    settings = make_settings(tmp_path, max_repair_attempts=1)

    result = run_repair(db, incident, rca, repo, provider, GIT, clock, settings, test_runner=TESTS)

    assert result.status == "failed"
    assert result.stop_reason == "attempts_exhausted"
    [attempt] = result.attempts
    assert attempt.status == "failed"
    assert attempt.tests_passed is False
    assert attempt.explanation == "Fixed. All tests pass."
    assert attempt.commit_sha is None
    assert "repair_succeeded" not in actions(db, incident)
    assert db.get(Incident, incident.id).status == "investigating"
    assert tree(source) == original


@needs_git
def test_regression_test_passing_before_patch_is_not_a_success(
    db, clock, setup, rca, source, original, tmp_path
):
    _, repo, incident = setup
    passing = "from calc import discount\n\n\ndef test_zero():\n    assert discount(0, 0) == 0\n"
    provider = FakeProvider([patch(FIXED, regression=passing)])
    settings = make_settings(tmp_path, max_repair_attempts=1)

    result = run_repair(db, incident, rca, repo, provider, GIT, clock, settings, test_runner=TESTS)

    [attempt] = result.attempts
    assert attempt.returncode == 0
    assert attempt.regression_test_failed_before is False
    assert attempt.status == "failed"
    assert result.status == "failed"
    assert tree(source) == original


def test_patch_touching_github_stops_with_security_risk(
    db, clock, setup, rca, source, original, tmp_path
):
    _, repo, incident = setup
    evil = PatchModel(
        files=[
            FileChange(path=".github/workflows/ci.yml", action=FileAction.CREATE, content="on: push\n"),
            FileChange(path="calc.py", action=FileAction.MODIFY, content=FIXED),
        ],
        explanation="All tests pass.",
    )
    provider = FakeProvider([evil, patch(FIXED)])
    tests = failing_tests()

    result = run_repair(db, incident, rca, repo, provider, FakeRunner(), clock, make_settings(tmp_path), test_runner=tests)

    assert result.status == "stopped"
    assert result.stop_reason == "security_risk"
    [attempt] = result.attempts
    assert attempt.status == "stopped" and attempt.stop_reason == "security_risk"
    assert "ci_config" in attempt.error
    assert len(provider.prompts) == 1
    assert tests.calls == []
    assert not (result.workspace_path / ".github").exists()
    assert "repair_stopped" in actions(db, incident)
    assert tree(source) == original


def test_exhausting_attempts_gives_attempts_exhausted(
    db, clock, setup, rca, source, original, tmp_path
):
    _, repo, incident = setup
    provider = FakeProvider([patch(WRONG, regression=None)])
    tests = failing_tests()
    settings = make_settings(tmp_path, max_repair_attempts=2)

    result = run_repair(db, incident, rca, repo, provider, FakeRunner(), clock, settings, test_runner=tests)

    assert result.status == "failed"
    assert result.stop_reason == "attempts_exhausted"
    assert [a.status for a in result.attempts] == ["failed", "failed"]
    assert result.attempts[-1].stop_reason == "attempts_exhausted"
    assert len(provider.prompts) == 2
    assert "assert 210 == 180" in provider.prompts[1].user
    assert [call.argv for call in tests.calls] == [TEST_COMMAND, TEST_COMMAND]
    assert "repair_failed" in actions(db, incident)
    assert tree(source) == original


def test_test_runner_timeout_gives_timeout(db, clock, setup, rca, source, original, tmp_path):
    _, repo, incident = setup
    provider = FakeProvider([patch(FIXED, regression=None)])
    tests = FakeRunner(
        default=CommandResult(returncode=-1, stderr="[sentinel] command timed out", timed_out=True)
    )
    settings = make_settings(tmp_path, repair_test_timeout_seconds=42)

    result = run_repair(db, incident, rca, repo, provider, FakeRunner(), clock, settings, test_runner=tests)

    assert result.status == "stopped"
    assert result.stop_reason == "timeout"
    [attempt] = result.attempts
    assert attempt.status == "stopped" and attempt.stop_reason == "timeout"
    assert tests.calls[0].argv == TEST_COMMAND
    assert tests.calls[0].timeout == 42
    assert len(provider.prompts) == 1
    assert tree(source) == original


def test_exceeding_application_budget_gives_budget_exhausted(
    db, clock, setup, rca, source, original, tmp_path
):
    application, repo, incident = setup
    application.budget_usd = 0.015
    db.commit()
    provider = FakeProvider([patch(WRONG, regression=None)], cost=0.01)
    settings = make_settings(tmp_path, max_repair_attempts=5)

    result = run_repair(db, incident, rca, repo, provider, FakeRunner(), clock, settings, test_runner=failing_tests())

    assert result.status == "stopped"
    assert result.stop_reason == "budget_exhausted"
    assert len(provider.prompts) == 2
    assert len(result.attempts) == 2
    assert result.attempts[-1].stop_reason == "budget_exhausted"
    assert db.get(Application, application.id).monthly_spend_usd == pytest.approx(0.02)
    assert tree(source) == original


def test_budget_already_spent_stops_before_calling_the_provider(
    db, clock, setup, rca, source, original, tmp_path
):
    application, repo, incident = setup
    application.budget_usd = 1.0
    application.monthly_spend_usd = 1.0
    db.commit()
    provider = FakeProvider([patch(FIXED)])

    result = run_repair(db, incident, rca, repo, provider, FakeRunner(), clock, make_settings(tmp_path), test_runner=failing_tests())

    assert result.stop_reason == "budget_exhausted"
    assert provider.prompts == []
    [attempt] = result.attempts
    assert attempt.status == "stopped"
    assert tree(source) == original


def test_time_budget_exhausted(db, clock, setup, rca, source, original, tmp_path):
    _, repo, incident = setup
    provider = FakeProvider([patch(WRONG, regression=None)], on_call=lambda: clock.advance(seconds=120))
    settings = make_settings(tmp_path, repair_time_budget_seconds=100, max_repair_attempts=3)

    result = run_repair(db, incident, rca, repo, provider, FakeRunner(), clock, settings, test_runner=failing_tests())

    assert result.stop_reason == "time_budget_exhausted"
    assert len(provider.prompts) == 1
    assert tree(source) == original


def test_unauthorized_repository_stops(db, clock, setup, rca, source, original, tmp_path):
    _, repo, incident = setup
    other = Application(name="Other", slug="other", ingest_key_hash="1" * 64)
    db.add(other)
    db.flush()
    foreign = AuthorizedRepository(
        application_id=other.id, name="other", local_path=str(source), test_command=TEST_COMMAND
    )
    db.add(foreign)
    db.commit()
    provider = FakeProvider([patch(FIXED)])
    git = FakeRunner()

    result = run_repair(db, incident, rca, foreign, provider, git, clock, make_settings(tmp_path))

    assert result.status == "stopped"
    assert result.stop_reason == "unauthorized_repo"
    assert provider.prompts == [] and git.calls == []
    assert result.workspace_path is None
    assert result.attempts[0].stop_reason == "unauthorized_repo"
    assert tree(source) == original


@pytest.mark.parametrize("provider", [None, UnconfiguredProvider()])
def test_provider_not_configured_stops(db, clock, setup, rca, source, original, tmp_path, provider):
    _, repo, incident = setup

    result = run_repair(db, incident, rca, repo, provider, FakeRunner(), clock, make_settings(tmp_path), test_runner=failing_tests())

    assert result.status == "stopped"
    assert result.stop_reason == "provider_not_configured"
    assert result.attempts[-1].status == "stopped"
    assert tree(source) == original


def test_secrets_in_test_output_are_redacted_before_feedback(db, clock, setup, rca, tmp_path):
    _, repo, incident = setup
    leaky = FakeRunner(
        default=CommandResult(returncode=1, stdout="password=hunter2-fictitious\n1 failed in 0.1s")
    )
    provider = FakeProvider([patch(WRONG, regression=None)])
    settings = make_settings(tmp_path, max_repair_attempts=2)

    result = run_repair(db, incident, rca, repo, provider, FakeRunner(), clock, settings, test_runner=leaky)

    assert "hunter2-fictitious" not in result.attempts[0].test_output
    assert "hunter2-fictitious" not in provider.prompts[1].user


def test_parse_test_summary():
    assert parse_test_summary("1 failed, 2 passed, 1 error in 0.12s") == {
        "failed": 1,
        "passed": 2,
        "errors": 1,
    }
    assert parse_test_summary("") == {}
