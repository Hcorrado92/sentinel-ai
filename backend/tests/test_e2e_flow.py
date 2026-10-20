"""End-to-end API flow: a real demo-app fault becomes a verified fix and a draft PR.

The authorized repository is a temporary git repository created from a copy
of demo-app/. The demo app runs in-process (imported from that copy, without
writing bytecode) and its ``SentinelReporter`` posts, through an ``httpx``
transport, to the Sentinel API under ``TestClient``. Repairs run on an isolated
copy with a real ``SubprocessRunner`` for git (behind the allow-list) and for
the repository's test command. ``gh`` (and the push it needs) goes to a
``FakeRunner``. The repository's own demo-app/ and .git are never touched.
"""

import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.clock import FakeClock
from app.config import Settings
from app.github.pr import find_workspace
from app.jobs.handlers import build_handlers
from app.main import create_app
from app.models import AuthorizedRepository, Event, Incident, PullRequest, RCAReport, RepairAttempt
from app.runner import AllowListRunner, CommandResult, FakeRunner, SubprocessRunner
from app.seed import DEMO_TEST_COMMAND, seed
from app.worker import run_once

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_ROOT = REPO_ROOT / "demo-app"
ADMIN_TOKEN = "fake-admin-token-for-e2e"
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
START = datetime(2024, 7, 1, 8, tzinfo=timezone.utc)
SENTINEL_URL = "http://sentinel.test"
GITHUB_REPO = "fictitious-org/demo-app"
PR_URL = "https://github.com/fictitious-org/demo-app/pull/42"
# Fictitious secret sent as the faulty quantity: it ends up in the exception message.
SECRET = "fictitious-e2e-pw-31337"
QUANTITY = f"password={SECRET}"
CORRELATION_ID = "e2e-request-0001"
SKIPPED_PARTS = {"__pycache__", ".pytest_cache", ".venv", ".git"}
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git binary not available")


def _skipped(path: Path) -> bool:
    return bool(SKIPPED_PARTS.intersection(path.parts)) or path.suffix == ".pyc"


def tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not _skipped(p.relative_to(root))
    }


class RecordingRunner:
    """Delegates to a real runner and records every argv/cwd it is asked to run."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls: list[tuple[list[str], str | None]] = []

    def run(self, argv, cwd=None, timeout=None, env=None) -> CommandResult:
        self.calls.append((list(argv), str(cwd) if cwd is not None else None))
        return self.inner.run(argv, cwd=cwd, timeout=timeout, env=env)


class PinnedPythonRunner(RecordingRunner):
    """Runs ``python`` as the interpreter running these tests (with the frozen deps).

    The repository's stored test command (``python -m pytest ...``) is recorded
    as requested; only the executable lookup is pinned, so the result does not
    depend on which ``python`` comes first on PATH.
    """

    def run(self, argv, cwd=None, timeout=None, env=None) -> CommandResult:
        self.calls.append((list(argv), str(cwd) if cwd is not None else None))
        if argv and argv[0] == "python":
            argv = [sys.executable, *argv[1:]]
        return self.inner.run(argv, cwd=cwd, timeout=timeout, env=env)


@pytest.fixture
def repo_snapshot():
    """The repository's demo-app/ must be byte-for-byte unchanged afterwards."""
    before = tree(DEMO_ROOT)
    assert "demo_app/main.py" in before
    yield before
    assert tree(DEMO_ROOT) == before


@pytest.fixture
def source(tmp_path) -> Path:
    """A temporary git repository created from a copy of demo-app/."""
    root = tmp_path / "authorized" / "demo-app"
    shutil.copytree(DEMO_ROOT, root, ignore=shutil.ignore_patterns(*SKIPPED_PARTS, "*.pyc"))
    git = AllowListRunner(SubprocessRunner(default_timeout=60))
    for args in (
        ["init", "-b", "main"],
        ["config", "user.name", "Fictitious Developer"],
        ["config", "user.email", "developer@example.invalid"],
        ["config", "commit.gpgsign", "false"],
        ["add", "-A"],
        ["commit", "--no-verify", "-m", "Demo shop"],
    ):
        result = git.run(["git", *args], cwd=root, timeout=60, env=GIT_ENV)
        assert result.returncode == 0, result.stderr
    return root


@pytest.fixture
def demo_modules(source, monkeypatch):
    """Import ``demo_app`` from the temporary copy (never from the repository)."""

    def forget() -> None:
        for name in [n for n in sys.modules if n == "demo_app" or n.startswith("demo_app.")]:
            del sys.modules[name]

    forget()
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    monkeypatch.syspath_prepend(str(source))
    from demo_app import main as demo_main
    from demo_app import sentinel_client

    assert Path(demo_main.__file__).resolve().is_relative_to(source.resolve())
    yield demo_main, sentinel_client
    forget()


@pytest.fixture
def settings(tmp_path, database_url) -> Settings:
    return Settings(
        database_url=database_url,
        workdir=str(tmp_path / "workdir"),
        admin_token=ADMIN_TOKEN,
        job_max_attempts=1,
        job_retry_backoff_seconds=0,
        max_repair_attempts=2,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(START)


@pytest.fixture
def git_runner() -> RecordingRunner:
    return RecordingRunner(SubprocessRunner(default_timeout=120))


@pytest.fixture
def test_runner() -> PinnedPythonRunner:
    return PinnedPythonRunner(SubprocessRunner(default_timeout=300))


@pytest.fixture
def gh() -> FakeRunner:
    return FakeRunner().script(
        ["gh", "pr", "create"], CommandResult(returncode=0, stdout=PR_URL + "\n")
    )


@pytest.fixture
def sentinel(settings, clock, git_runner, test_runner, gh):
    application = create_app(settings, clock=clock)
    # Default (mock) provider; real git and test subprocesses behind the allow-list.
    application.state.job_handlers = build_handlers(
        settings, git_runner=git_runner, test_runner=test_runner
    )
    application.state.git_runner = gh
    yield application
    application.state.engine.dispose()


@pytest.fixture
def client(sentinel):
    with TestClient(sentinel) as test_client:
        yield test_client


def _reporter_transport(sentinel_app, responses: list[httpx.Response]) -> httpx.MockTransport:
    """Forward the demo reporter's HTTP requests to the Sentinel app in-process."""
    forwarder = TestClient(sentinel_app)

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "sentinel.test"
        response = forwarder.post(
            request.url.path,
            content=request.read(),
            headers={
                "Content-Type": request.headers["content-type"],
                "X-Sentinel-Key": request.headers["x-sentinel-key"],
            },
        )
        responses.append(response)
        return httpx.Response(
            response.status_code,
            content=response.content,
            headers={"Content-Type": response.headers.get("content-type", "application/json")},
        )

    return httpx.MockTransport(handle)


def _process(sentinel, clock):
    return run_once(
        sentinel.state.session_factory, sentinel.state.job_handlers, clock=clock, backoff_seconds=0
    )


def _within(path: str | None, root: Path) -> bool:
    return path is not None and Path(path).resolve().is_relative_to(root.resolve())


def test_demo_fault_flows_to_a_verified_repair_and_a_draft_pr(
    repo_snapshot, tmp_path, source, demo_modules, sentinel, client, clock,
    settings, git_runner, test_runner, gh,
):
    demo_main, sentinel_client = demo_modules
    source_before = tree(source)

    # --- seed: the demo application and its authorized (temporary) repository ---
    with sentinel.state.session_factory() as db:
        seeded = seed(db, demo_app_path=source, clock=clock)
        repo = db.get(AuthorizedRepository, seeded.repository_id)
        repo.github_repo = GITHUB_REPO
        db.commit()
        assert repo.test_command == DEMO_TEST_COMMAND
    assert seeded.ingest_key

    # --- the real demo fault, reported to Sentinel by the demo's reporter ---
    clock.advance(minutes=1)
    responses: list[httpx.Response] = []
    reporter = sentinel_client.SentinelReporter(
        SENTINEL_URL,
        seeded.ingest_key,
        "development",
        transport=_reporter_transport(sentinel, responses),
    )
    demo = demo_main.create_app(
        demo_main.DemoSettings(environment="development", faults_enabled=True), reporter=reporter
    )
    with TestClient(demo, raise_server_exceptions=False) as demo_client:
        response = demo_client.post(
            "/orders",
            json={"product": "widget", "quantity": QUANTITY},
            headers={"X-Request-ID": CORRELATION_ID},
        )
    assert response.status_code == 500
    assert [r.status_code for r in responses] == [202]
    accepted = responses[0].json()
    assert accepted["deduplicated"] is False
    incident_id = accepted["incident_id"]

    # --- exactly one incident, count 1, redacted data ---
    incidents = client.get("/api/v1/incidents", headers=AUTH).json()
    assert incidents["total"] == 1
    [incident] = incidents["items"]
    assert incident["id"] == incident_id
    assert incident["event_count"] == 1
    assert incident["exception_type"] == "ValueError"
    assert incident["application_name"] == "demo-app"
    assert SECRET not in incident["title"]

    events = client.get(f"/api/v1/incidents/{incident_id}/events", headers=AUTH).json()
    assert len(events) == 1
    [event] = events
    assert event["correlation_id"] == CORRELATION_ID
    assert event["metadata"] == {"method": "POST", "path": "/orders"}
    assert "password=[REDACTED]" in event["message"]
    assert "invalid literal for int()" in event["message"]
    assert "in _parse_quantity" in event["stack_trace"]
    assert "password=[REDACTED]" in event["stack_trace"]
    with sentinel.state.session_factory() as db:
        assert db.query(Incident).count() == 1
        stored = db.query(Event).one()
        for text in (stored.message, stored.stack_trace, str(stored.metadata_json)):
            assert SECRET not in text

    # --- RCA from the mock provider ---
    clock.advance(minutes=1)
    assert client.post(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH).status_code == 202
    clock.advance(seconds=5)
    rca_job = _process(sentinel, clock)
    assert rca_job.type == "rca" and rca_job.status == "succeeded", rca_job.error
    rca = client.get(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH).json()
    assert rca["status"] == "completed"
    assert rca["provider"] == "mock" and rca["is_mock"] is True
    assert rca["report"]["suspect_locations"][0]["file"] == "demo_app/main.py"
    assert rca["report"]["suspect_locations"][0]["function"] == "_parse_quantity"
    assert all(h["status"] == "hypothesis" for h in rca["report"]["hypotheses"])

    # --- repair on an isolated copy, verified by the repository's own tests ---
    clock.advance(minutes=1)
    assert client.post(f"/api/v1/incidents/{incident_id}/repair", headers=AUTH).status_code == 202
    clock.advance(seconds=5)
    repair_job = _process(sentinel, clock)
    assert repair_job.type == "repair" and repair_job.status == "succeeded", repair_job.error

    repairs = client.get(f"/api/v1/incidents/{incident_id}/repairs", headers=AUTH).json()
    attempt = repairs[-1]
    branch = f"sentinel/fix-{incident_id}-1"
    assert attempt["status"] == "succeeded", attempt["error"]
    assert attempt["returncode"] == 0
    assert attempt["tests_passed"] is True
    assert attempt["regression_test_failed_before"] is True
    assert attempt["test_command"] == ["python", "-m", "pytest", "-q", "-m", "not fault_reproduction"]
    assert attempt["branch_name"] == branch
    assert "passed" in attempt["test_output"]
    assert "demo_app/main.py" in attempt["diff"]
    assert "tests/test_regression_quantity_validation.py" in attempt["diff"]
    assert attempt["commit_sha"]

    workspace = find_workspace(settings.workdir, incident_id, branch)
    assert workspace is not None
    assert _within(str(workspace), tmp_path / "workdir")
    # The full test command ran in the isolated copy (not in the source).
    full_runs = [cwd for argv, cwd in test_runner.calls if argv == DEMO_TEST_COMMAND]
    assert full_runs and all(Path(cwd).resolve() == workspace for cwd in full_runs)
    head = git_runner.inner.run(["git", "rev-parse", "HEAD"], cwd=workspace, env=GIT_ENV)
    assert head.stdout.strip() == attempt["commit_sha"]
    current = git_runner.inner.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=workspace, env=GIT_ENV
    )
    assert current.stdout.strip() == branch
    assert "Sentinel fix" in (workspace / "demo_app" / "main.py").read_text(encoding="utf-8")

    incident_detail = client.get(f"/api/v1/incidents/{incident_id}", headers=AUTH).json()
    assert incident_detail["status"] == "fix_proposed"
    confirmed = client.get(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH).json()
    assert confirmed["report"]["hypotheses"][0]["status"] == "confirmed"

    # --- draft PR: prepared with publication disabled (the default) ---
    assert settings.github_publish is False
    clock.advance(minutes=1)
    prepared = client.post(f"/api/v1/incidents/{incident_id}/pull-request", headers=AUTH)
    assert prepared.status_code == 201, prepared.text
    pr = prepared.json()
    assert pr["status"] == "prepared"
    assert pr["draft"] is True and pr["url"] is None
    assert pr["branch"] == branch and pr["base"] == "main"
    assert gh.calls == []
    assert SECRET not in pr["body"] and SECRET not in pr["title"]
    assert "password=[REDACTED]" in pr["body"]
    assert "python -m pytest -q -m not fault_reproduction" in pr["body"]

    # --- with publication enabled the fake gh gets a draft PR, never a merge ---
    settings.github_publish = True
    clock.advance(minutes=1)
    published = client.post(f"/api/v1/incidents/{incident_id}/pull-request", headers=AUTH)
    assert published.status_code == 201, published.text
    assert published.json()["id"] == pr["id"]
    assert published.json()["status"] == "published"
    assert published.json()["url"] == PR_URL

    gh_calls = [argv for argv in gh.argvs if argv[0] == "gh"]
    assert len(gh_calls) == 1
    assert gh_calls[0][:3] == ["gh", "pr", "create"]
    assert "--draft" in gh_calls[0]
    assert gh_calls[0][gh_calls[0].index("--head") + 1] == branch
    assert gh_calls[0][gh_calls[0].index("--base") + 1] == "main"
    pushes = [argv for argv in gh.argvs if argv[:2] == ["git", "push"]]
    assert len(pushes) == 1 and pushes[0][-1] == f"{branch}:{branch}"
    all_argvs = gh.argvs + [argv for argv, _ in git_runner.calls]
    assert all("merge" not in argv for argv in all_argvs)
    assert all(argv[:2] != ["gh", "pr"] or argv[2] == "create" for argv in all_argvs)
    for argv in gh.argvs:
        assert all(SECRET not in arg for arg in argv)

    # --- timeline: received -> grouped -> rca -> repair -> pr, chronologically ---
    timeline = client.get(f"/api/v1/incidents/{incident_id}/timeline", headers=AUTH).json()
    types = [entry["type"] for entry in timeline]
    milestones = ["event_received", "incident_grouped", "rca_completed", "repair_succeeded", "pr_prepared"]
    positions = [types.index(name) for name in milestones]
    assert positions == sorted(positions), types
    assert types.index("pr_published") > types.index("pr_prepared")
    stamps = [datetime.fromisoformat(str(entry["timestamp"])) for entry in timeline]
    assert stamps == sorted(stamps)
    milestone_stamps = [stamps[i] for i in positions]
    assert milestone_stamps[0] < milestone_stamps[2] < milestone_stamps[3] < milestone_stamps[4]

    # --- the audit log has an entry for each decision ---
    audit = client.get(f"/api/v1/incidents/{incident_id}/audit", headers=AUTH).json()
    actions = [entry["action"] for entry in audit]
    for action in (
        "event_received",
        "incident_grouped",
        "rca_requested",
        "rca_started",
        "rca_completed",
        "repair_requested",
        "repair_started",
        "repair_workspace_created",
        "repair_attempt_started",
        "repair_attempt_succeeded",
        "repair_succeeded",
        "rca_hypothesis_confirmed",
        "pr_prepared",
        "pr_published",
    ):
        assert action in actions, (action, actions)
    assert "pr_refused" not in actions

    # --- nothing secret was persisted anywhere along the flow ---
    with sentinel.state.session_factory() as db:
        texts = [i.title for i in db.query(Incident)]
        texts += [str(r.report) + (r.summary or "") for r in db.query(RCAReport)]
        texts += [(a.diff or "") + (a.test_output or "") for a in db.query(RepairAttempt)]
        texts += [p.body + p.title for p in db.query(PullRequest)]
    assert all(SECRET not in text for text in texts)

    # --- isolation: the source repository is unchanged; nothing ran in this repo ---
    assert tree(source) == source_before
    for _, cwd in git_runner.calls + test_runner.calls:
        assert _within(cwd, tmp_path), cwd
        assert not _within(cwd, DEMO_ROOT), cwd
        assert Path(cwd).resolve() != REPO_ROOT.resolve(), cwd
    for call in gh.calls:
        assert _within(call.cwd, tmp_path / "workdir"), call.cwd
