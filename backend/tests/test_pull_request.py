from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.ai.mock import MockProvider, MockRule
from app.clock import FakeClock
from app.config import Settings
from app.github.pr import PRNotAllowed, prepare_pull_request
from app.jobs.handlers import build_handlers
from app.main import create_app
from app.models import (
    Application,
    AuditLog,
    AuthorizedRepository,
    Event,
    Incident,
    PullRequest,
    RCAReport,
    RepairAttempt,
)
from app.repair.patch import FileAction, FileChange, PatchModel
from app.runner import AllowListRunner, CommandResult, FakeRunner
from app.security.redaction import redact_text
from app.worker import run_once

ADMIN_TOKEN = "fake-admin-token-for-tests"
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
START = datetime(2024, 4, 2, 9, tzinfo=timezone.utc)
TEST_COMMAND = ["python", "-m", "pytest", "-q"]
GITHUB_REPO = "fictitious-org/demo-shop"
PR_URL = "https://github.com/fictitious-org/demo-shop/pull/7"

# Fictitious secrets planted in event data, the RCA and the repair attempt.
SECRETS = (
    "fictitious-hunter2",
    "fictitious-tok-123",
    "fictitious-bearer-abc",
    "fictitious-key-999",
    "fictitious-pw",
)

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
    application.state.git_runner = FakeRunner().script(
        ["gh", "pr", "create"], CommandResult(returncode=0, stdout=PR_URL + "\n")
    )
    yield application
    application.state.engine.dispose()


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def pr_runner(app) -> FakeRunner:
    return app.state.git_runner


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


def _seed_incident(app, clock, repo_root, github_repo: str | None = GITHUB_REPO) -> int:
    with app.state.session_factory() as db:
        application = Application(name="Demo Shop", slug="demo-shop", ingest_key_hash="0" * 64)
        db.add(application)
        db.flush()
        db.add(
            AuthorizedRepository(
                application_id=application.id,
                name="demo-app",
                local_path=str(repo_root),
                test_command=TEST_COMMAND,
                github_repo=github_repo,
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
                event_id="evt-demo-shop",
                timestamp=clock.now(),
                exception_type="TypeError",
                # Stored directly (bypassing ingestion) so the PR redaction is exercised.
                message=(
                    "'NoneType' object is not subscriptable password=fictitious-hunter2 "
                    "token=fictitious-tok-123"
                ),
                stack_trace=STACK,
                environment="production",
                metadata_json={},
            )
        )
        db.commit()
        return incident.id


@pytest.fixture
def incident_id(app, clock, repo_root) -> int:
    return _seed_incident(app, clock, repo_root)


def _handlers(settings, clock, tests: FakeRunner):
    provider = MockProvider(
        rules=[
            MockRule(
                "scripted-fix",
                lambda prompt, schema: prompt.task == "repair",
                lambda prompt, schema: SCRIPTED_PATCH,
            )
        ]
    )
    git = (
        FakeRunner()
        .script(["git", "diff"], CommandResult(returncode=0, stdout=DIFF))
        .script(["git", "rev-parse"], CommandResult(returncode=0, stdout="0123abcd\n"))
    )
    return build_handlers(settings, provider_factory=lambda: provider, git_runner=git, test_runner=tests)


def passing_tests() -> FakeRunner:
    return FakeRunner(
        results=[
            CommandResult(returncode=1, stdout="E   TypeError\n1 failed in 0.01s"),
            CommandResult(returncode=0, stdout="2 passed in 0.02s"),
        ]
    )


def failing_tests() -> FakeRunner:
    return FakeRunner(default=CommandResult(returncode=1, stdout="E   assert 0 == 4\n1 failed"))


def _run_repair(client, app, clock, handlers, incident_id) -> None:
    def process():
        return run_once(app.state.session_factory, handlers, clock=clock, backoff_seconds=0)

    assert client.post(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH).status_code == 202
    assert process().status == "succeeded"
    assert client.post(f"/api/v1/incidents/{incident_id}/repair", headers=AUTH).status_code == 202
    process()


def _audit_actions(app, incident_id) -> list[str]:
    with app.state.session_factory() as db:
        return [
            entry.action
            for entry in db.query(AuditLog).filter_by(incident_id=incident_id).order_by(AuditLog.id)
        ]


def _gh_or_push(argvs: list[list[str]]) -> list[list[str]]:
    return [argv for argv in argvs if argv[0] == "gh" or argv[:2] == ["git", "push"]]


# --- refusals --------------------------------------------------------------------


def test_pull_request_endpoints_require_admin(client, incident_id):
    assert client.post(f"/api/v1/incidents/{incident_id}/pull-request").status_code == 401
    assert client.get(f"/api/v1/incidents/{incident_id}/pull-request").status_code == 401


def test_unknown_incident_is_404(client):
    assert client.post("/api/v1/incidents/999/pull-request", headers=AUTH).status_code == 404
    assert client.get("/api/v1/incidents/999/pull-request", headers=AUTH).status_code == 404


def test_no_repair_attempt_is_409(client, app, pr_runner, incident_id):
    response = client.post(f"/api/v1/incidents/{incident_id}/pull-request", headers=AUTH)
    assert response.status_code == 409
    assert pr_runner.calls == []
    assert "pr_refused" in _audit_actions(app, incident_id)
    assert client.get(f"/api/v1/incidents/{incident_id}/pull-request", headers=AUTH).status_code == 404


def test_failed_repair_is_409_and_issues_no_gh_command(
    client, app, settings, clock, pr_runner, incident_id
):
    settings.github_publish = True
    _run_repair(client, app, clock, _handlers(settings, clock, failing_tests()), incident_id)

    response = client.post(f"/api/v1/incidents/{incident_id}/pull-request", headers=AUTH)
    assert response.status_code == 409
    assert "successful repair" in response.json()["detail"]
    assert pr_runner.calls == []
    actions = _audit_actions(app, incident_id)
    assert "pr_refused" in actions
    assert "pr_prepared" not in actions
    with app.state.session_factory() as db:
        assert db.query(PullRequest).count() == 0


def test_prepare_refuses_attempt_without_green_tests(app, clock, repo_root, settings):
    incident_id = _seed_incident(app, clock, repo_root)
    runner = FakeRunner()
    with app.state.session_factory() as db:
        incident = db.get(Incident, incident_id)
        repo = db.query(AuthorizedRepository).one()
        attempt = RepairAttempt(
            incident_id=incident_id,
            repository_id=repo.id,
            branch_name=f"sentinel/fix-{incident_id}-1",
            status="succeeded",
            returncode=1,
            tests_passed=False,
        )
        db.add(attempt)
        db.commit()
        with pytest.raises(PRNotAllowed):
            prepare_pull_request(
                attempt, incident, None, repo, runner, settings, db=db, clock=clock
            )
    assert runner.calls == []
    assert "pr_refused" in _audit_actions(app, incident_id)


# --- prepared (publication disabled) --------------------------------------------


def test_successful_repair_prepares_pr_without_publishing(
    client, app, settings, clock, pr_runner, incident_id
):
    assert settings.github_publish is False
    _run_repair(client, app, clock, _handlers(settings, clock, passing_tests()), incident_id)
    pr_runner.calls.clear()

    response = client.post(f"/api/v1/incidents/{incident_id}/pull-request", headers=AUTH)
    assert response.status_code == 201
    pr = response.json()
    assert pr["status"] == "prepared"
    assert pr["draft"] is True
    assert pr["url"] is None
    assert pr["branch"] == f"sentinel/fix-{incident_id}-1"
    assert pr["base"] == "main"
    assert f"#{incident_id}" in pr["title"]

    rca = client.get(f"/api/v1/incidents/{incident_id}/rca", headers=AUTH).json()
    body = pr["body"]
    assert redact_text(rca["summary"]) in body
    assert "demo_app/main.py" in body
    assert "+    if item is None:" in body
    assert "2 passed" in body
    assert "python -m pytest -q" in body

    assert _gh_or_push(pr_runner.argvs) == []
    assert pr_runner.calls == []
    assert "pr_prepared" in _audit_actions(app, incident_id)

    fetched = client.get(f"/api/v1/incidents/{incident_id}/pull-request", headers=AUTH)
    assert fetched.status_code == 200
    assert fetched.json()["id"] == pr["id"]
    assert fetched.json()["status"] == "prepared"

    timeline = client.get(f"/api/v1/incidents/{incident_id}/timeline", headers=AUTH).json()
    assert "pr_prepared" in [entry["type"] for entry in timeline]


def test_publish_enabled_without_github_slug_stays_prepared(
    client, app, settings, clock, pr_runner, repo_root
):
    settings.github_publish = True
    incident_id = _seed_incident(app, clock, repo_root, github_repo=None)
    _run_repair(client, app, clock, _handlers(settings, clock, passing_tests()), incident_id)

    response = client.post(f"/api/v1/incidents/{incident_id}/pull-request", headers=AUTH)
    assert response.status_code == 201
    assert response.json()["status"] == "prepared"
    assert pr_runner.calls == []


# --- published (publication enabled) --------------------------------------------


def test_publish_pushes_fix_branch_and_creates_draft_pr(
    client, app, settings, clock, pr_runner, incident_id
):
    settings.github_publish = True
    _run_repair(client, app, clock, _handlers(settings, clock, passing_tests()), incident_id)

    response = client.post(f"/api/v1/incidents/{incident_id}/pull-request", headers=AUTH)
    assert response.status_code == 201, response.text
    pr = response.json()
    branch = f"sentinel/fix-{incident_id}-1"
    assert pr["status"] == "published"
    assert pr["url"] == PR_URL

    argvs = pr_runner.argvs
    pushes = [argv for argv in argvs if argv[:2] == ["git", "push"]]
    assert len(pushes) == 1
    push = pushes[0]
    assert push[-1] == f"{branch}:{branch}"
    assert GITHUB_REPO in push[2]
    for refspec in push[3:]:
        assert refspec.partition(":")[2] not in ("main", "master")

    gh_calls = [argv for argv in argvs if argv[0] == "gh"]
    assert len(gh_calls) == 1
    gh = gh_calls[0]
    assert gh[:3] == ["gh", "pr", "create"]
    assert "--draft" in gh
    assert gh[gh.index("--base") + 1] == "main"
    assert gh[gh.index("--head") + 1] == branch
    assert gh[gh.index("--title") + 1] == pr["title"]
    assert "--body-file" in gh

    assert all("merge" not in argv for argv in argvs)
    assert all(argv[:2] != ["gh", "pr"] or argv[2] == "create" for argv in argvs)

    actions = _audit_actions(app, incident_id)
    assert "pr_prepared" in actions
    assert "pr_published" in actions

    repairs = client.get(f"/api/v1/incidents/{incident_id}/repairs", headers=AUTH).json()
    assert repairs[-1]["pr_url"] == PR_URL
    fetched = client.get(f"/api/v1/incidents/{incident_id}/pull-request", headers=AUTH).json()
    assert fetched["status"] == "published"
    assert fetched["url"] == PR_URL

    # Asking again returns the published PR without issuing more commands.
    count = len(pr_runner.calls)
    again = client.post(f"/api/v1/incidents/{incident_id}/pull-request", headers=AUTH)
    assert again.status_code == 201
    assert again.json()["id"] == pr["id"]
    assert len(pr_runner.calls) == count


def test_publish_never_pushes_the_base_branch(app, clock, repo_root, settings, tmp_path):
    settings.github_publish = True
    incident_id = _seed_incident(app, clock, repo_root)
    workspace = tmp_path / "workdir" / f"demo-app-incident-{incident_id}-1-abcd1234"
    workspace.mkdir(parents=True)
    runner = FakeRunner()
    with app.state.session_factory() as db:
        incident = db.get(Incident, incident_id)
        repo = db.query(AuthorizedRepository).one()
        # A misconfigured repository whose base branch is the fix branch itself.
        repo.base_branch = f"sentinel/fix-{incident_id}-1"
        attempt = RepairAttempt(
            incident_id=incident_id,
            repository_id=repo.id,
            branch_name=f"sentinel/fix-{incident_id}-1",
            status="succeeded",
            returncode=0,
            tests_passed=True,
        )
        db.add(attempt)
        db.commit()
        pr = prepare_pull_request(
            attempt, incident, None, repo, AllowListRunner(runner), settings, db=db, clock=clock
        )
        assert pr.status == "publish_failed"
    assert _gh_or_push(runner.argvs) == []


# --- redaction -------------------------------------------------------------------


def test_pr_body_redacts_secrets_from_event_data(
    client, app, settings, clock, pr_runner, incident_id
):
    settings.github_publish = True
    _run_repair(client, app, clock, _handlers(settings, clock, passing_tests()), incident_id)
    with app.state.session_factory() as db:
        report = db.query(RCAReport).filter_by(incident_id=incident_id).one()
        report.summary = f"{report.summary} Authorization: Bearer fictitious-bearer-abc"
        attempt = db.query(RepairAttempt).filter_by(status="succeeded").one()
        attempt.explanation = "Return 0. api_key=fictitious-key-999"
        attempt.diff = DIFF + "+DB_URL = 'postgres://user:fictitious-pw@db.example.invalid/x'\n"
        db.commit()

    response = client.post(f"/api/v1/incidents/{incident_id}/pull-request", headers=AUTH)
    assert response.status_code == 201
    pr = response.json()
    assert "[REDACTED]" in pr["body"]
    assert "'NoneType' object is not subscriptable" in pr["body"]
    for secret in SECRETS:
        assert secret not in pr["body"]
        assert secret not in pr["title"]

    # Nothing secret reaches the gh command line either.
    for argv in pr_runner.argvs:
        for secret in SECRETS:
            assert all(secret not in arg for arg in argv)
    with app.state.session_factory() as db:
        stored = db.query(PullRequest).one()
        for secret in SECRETS:
            assert secret not in stored.body
