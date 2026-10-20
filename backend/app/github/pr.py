"""Draft pull requests for verified repairs.

``prepare_pull_request`` refuses (raises ``PRNotAllowed`` and records a
``pr_refused`` audit entry) unless the repair attempt succeeded with green
tests. Otherwise it builds a redacted title and body (incident summary, RCA
report, diff and test results) and stores a ``PullRequest`` with status
``prepared`` (audit ``pr_prepared``).

Publication is disabled by default. Only when ``settings.github_publish`` is
true and the authorized repository has a ``github_repo`` slug does it push the
fix branch from the isolated copy (never the base branch) and run
``gh pr create --draft`` through an ``AllowListRunner``; the URL is stored with
status ``published`` (audit ``pr_published``). No merge command is ever used.
"""

import os
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clock import Clock
from app.config import Settings
from app.models import (
    AuditLog,
    AuthorizedRepository,
    Event,
    Incident,
    PullRequest,
    RCAReport,
    RepairAttempt,
)
from app.repair.workspace import BRANCH_PREFIX
from app.runner import (
    DEFAULT_PROTECTED_BRANCHES,
    AllowListRunner,
    CommandNotAllowed,
    CommandRunner,
)
from app.security.redaction import redact_text

PR_ACTOR = "github"
GIT_TIMEOUT_SECONDS = 120.0
GH_TIMEOUT_SECONDS = 120.0
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}
# Server-side credentials forwarded to ``gh`` when they are configured.
GH_CREDENTIAL_VARS = ("GH_TOKEN", "GITHUB_TOKEN")
MAX_TITLE_CHARS = 200
MAX_DIFF_CHARS = 30_000
MAX_TEST_OUTPUT_CHARS = 10_000
MAX_ERROR_CHARS = 2000
_PR_FILES_DIR = ".sentinel-pr"

STATUS_PREPARED = "prepared"
STATUS_PUBLISHED = "published"
STATUS_PUBLISH_FAILED = "publish_failed"

_GITHUB_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-]{0,38}/[A-Za-z0-9._\-]{1,100}$")
_URL = re.compile(r"https://\S+")


class PRNotAllowed(Exception):
    """The pull request must not be prepared (e.g. the repair is not verified)."""


class _PublishError(Exception):
    pass


def _audit(
    db: Session, incident: Incident, clock: Clock, action: str, details: dict[str, Any]
) -> None:
    db.add(
        AuditLog(
            application_id=incident.application_id,
            incident_id=incident.id,
            actor=PR_ACTOR,
            action=action,
            details=details,
            created_at=clock.now(),
        )
    )


def refusal_reason(attempt: RepairAttempt | None, incident: Incident) -> str | None:
    """Why a PR cannot be prepared from ``attempt`` (``None`` when it can)."""
    if attempt is None:
        return "there is no repair attempt for this incident"
    if attempt.incident_id != incident.id:
        return "the repair attempt belongs to another incident"
    if attempt.status != "succeeded":
        return f"the repair attempt did not succeed (status {attempt.status})"
    if attempt.tests_passed is not True or attempt.returncode != 0:
        return "the repository tests did not pass"
    if not attempt.branch_name or not attempt.branch_name.startswith(BRANCH_PREFIX):
        return "the repair attempt has no Sentinel fix branch"
    return None


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... {len(text) - limit} characters truncated ...]"


def _yes_no(value: bool | None) -> str:
    return "unknown" if value is None else ("yes" if value else "no")


def _rca_section(rca: RCAReport | None) -> list[str]:
    if rca is None:
        return ["No RCA report is available."]
    report = rca.report or {}
    lines = [f"**Summary:** {rca.summary or report.get('summary', '')}", ""]
    hypotheses = report.get("hypotheses") or []
    if hypotheses:
        lines.append("**Hypotheses:**")
        for hypothesis in hypotheses:
            confidence = hypothesis.get("confidence")
            confidence_text = f"{confidence:.0%}" if isinstance(confidence, (int, float)) else "?"
            lines.append(
                f"- [{hypothesis.get('status', 'hypothesis')}] {hypothesis.get('title', '')} "
                f"(confidence {confidence_text})"
            )
            if hypothesis.get("description"):
                lines.append(f"  {hypothesis['description']}")
            for evidence in hypothesis.get("evidence") or []:
                lines.append(f"  - Evidence: {evidence}")
        lines.append("")
    locations = report.get("suspect_locations") or []
    if locations:
        lines.append("**Suspect locations:**")
        for location in locations:
            function = f" `{location['function']}`" if location.get("function") else ""
            lines.append(f"- `{location.get('file', '')}`{function}: {location.get('reason', '')}")
        lines.append("")
    uncertainties = report.get("uncertainties") or []
    if uncertainties:
        lines.append("**Uncertainties:**")
        lines.extend(f"- {item}" for item in uncertainties)
        lines.append("")
    return lines


def build_title(incident: Incident) -> str:
    title = f"Sentinel AI: fix incident #{incident.id} - {incident.title or incident.exception_type}"
    return redact_text(" ".join(title.split()))[:MAX_TITLE_CHARS]


def build_body(
    attempt: RepairAttempt,
    incident: Incident,
    rca: RCAReport | None,
    latest_event: Event | None,
) -> str:
    """Markdown PR body; every part is redacted."""
    lines = [
        "> Draft pull request prepared by Sentinel AI. Review it before merging;",
        "> Sentinel never merges or deploys changes.",
        "",
        "## Incident",
        f"- **Incident:** #{incident.id} {incident.title}",
        f"- **Exception:** {incident.exception_type}",
        f"- **Severity:** {incident.severity}",
        f"- **Occurrences:** {incident.event_count}",
        f"- **First seen:** {incident.first_seen}",
        f"- **Last seen:** {incident.last_seen}",
    ]
    if latest_event is not None:
        lines.append(f"- **Environment:** {latest_event.environment}")
        lines.append(f"- **Latest message:** {latest_event.message}")
    lines += ["", "## Root cause analysis", *_rca_section(rca)]
    if attempt.explanation:
        lines += ["## Change", attempt.explanation, ""]
    lines += [
        "## Diff",
        "```diff",
        _truncate(attempt.diff or "(no diff recorded)", MAX_DIFF_CHARS),
        "```",
        "",
        "## Test results",
        f"- **Command:** `{' '.join(attempt.test_command or [])}`",
        f"- **Return code:** {attempt.returncode}",
        f"- **Tests passed:** {'yes' if attempt.tests_passed else 'no'}",
        f"- **Regression test failed before the patch:** "
        f"{_yes_no(attempt.regression_test_failed_before)}",
    ]
    if attempt.test_summary:
        summary = ", ".join(f"{count} {label}" for label, count in attempt.test_summary.items())
        lines.append(f"- **Summary:** {summary}")
    lines += [
        "",
        "```",
        _truncate(attempt.test_output or "(no output recorded)", MAX_TEST_OUTPUT_CHARS),
        "```",
        "",
        f"Branch `{attempt.branch_name}`, repair attempt {attempt.attempt_number}"
        f" (id {attempt.id}), commit `{attempt.commit_sha or 'unknown'}`.",
    ]
    return redact_text("\n".join(lines))


def find_workspace(workdir: str | os.PathLike[str], incident_id: int, branch: str) -> Path | None:
    """Locate the isolated copy that holds ``branch`` under the workdir."""
    match = re.fullmatch(re.escape(BRANCH_PREFIX) + r"(\d+)-(\d+)", branch)
    if match is None or int(match.group(1)) != incident_id:
        return None
    root = Path(workdir).resolve()
    if not root.is_dir():
        return None
    for candidate in sorted(root.glob(f"*-incident-{incident_id}-{match.group(2)}-*")):
        resolved = candidate.resolve()
        if resolved.is_dir() and root in resolved.parents:
            return resolved
    return None


def _gh_env() -> dict[str, str]:
    env = {"GH_PROMPT_DISABLED": "1", **GIT_ENV}
    for name in GH_CREDENTIAL_VARS:
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def _run(
    runner: CommandRunner, argv: list[str], cwd: Path, timeout: float, env: Mapping[str, str]
) -> str:
    try:
        result = runner.run(argv, cwd=str(cwd), timeout=timeout, env=env)
    except CommandNotAllowed as exc:
        raise _PublishError(f"command refused: {exc}") from exc
    if result.timed_out:
        raise _PublishError(f"{argv[0]} {argv[1]} timed out")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise _PublishError(f"{argv[0]} {argv[1]} failed ({result.returncode}): {detail}")
    return result.stdout or ""


def _publish(
    pr: PullRequest,
    workspace: Path,
    runner: AllowListRunner,
    settings: Settings,
) -> str:
    if pr.branch == pr.base or not pr.branch.startswith(BRANCH_PREFIX):
        raise _PublishError(f"refusing to push branch {pr.branch!r}")
    remote = f"https://github.com/{pr.github_repo}.git"
    _run(
        runner,
        ["git", "push", remote, f"{pr.branch}:{pr.branch}"],
        workspace,
        GIT_TIMEOUT_SECONDS,
        GIT_ENV,
    )

    files_dir = Path(settings.workdir).resolve() / _PR_FILES_DIR
    files_dir.mkdir(parents=True, exist_ok=True)
    body_file = files_dir / f"pr-{pr.id}.md"
    body_file.write_text(pr.body, encoding="utf-8")
    try:
        stdout = _run(
            runner,
            [
                "gh", "pr", "create",
                "--draft",
                "--repo", pr.github_repo or "",
                "--base", pr.base,
                "--head", pr.branch,
                "--title", pr.title,
                "--body-file", str(body_file),
            ],
            workspace,
            GH_TIMEOUT_SECONDS,
            _gh_env(),
        )
    finally:
        body_file.unlink(missing_ok=True)
    urls = _URL.findall(stdout)
    if not urls:
        raise _PublishError("gh pr create did not return a pull request URL")
    return urls[-1]


def _latest_event(db: Session, incident_id: int) -> Event | None:
    return db.scalars(
        select(Event)
        .where(Event.incident_id == incident_id)
        .order_by(Event.timestamp.desc(), Event.id.desc())
        .limit(1)
    ).first()


def prepare_pull_request(
    attempt: RepairAttempt | None,
    incident: Incident,
    rca: RCAReport | None,
    repo: AuthorizedRepository | None,
    runner: CommandRunner,
    settings: Settings,
    *,
    db: Session,
    clock: Clock,
) -> PullRequest:
    """Prepare (and, when enabled, publish as a draft) the PR for ``attempt``."""
    reason = refusal_reason(attempt, incident)
    if reason is None and (repo is None or repo.application_id != incident.application_id):
        reason = "the repository is not authorized for this application"
    if reason is not None:
        _audit(
            db, incident, clock, "pr_refused",
            {"reason": reason, "repair_attempt_id": attempt.id if attempt else None},
        )
        db.commit()
        raise PRNotAllowed(reason)

    pr = db.scalar(
        select(PullRequest)
        .where(PullRequest.repair_attempt_id == attempt.id)
        .order_by(PullRequest.id.desc())
        .limit(1)
    )
    if pr is not None and pr.status == STATUS_PUBLISHED:
        return pr
    if pr is None:
        github_repo = repo.github_repo if repo.github_repo else None
        pr = PullRequest(
            incident_id=incident.id,
            repair_attempt_id=attempt.id,
            repository_id=repo.id,
            status=STATUS_PREPARED,
            branch=attempt.branch_name,
            base=repo.base_branch or "main",
            title=build_title(incident),
            body=build_body(attempt, incident, rca, _latest_event(db, incident.id)),
            github_repo=github_repo,
            created_at=clock.now(),
        )
        db.add(pr)
        db.flush()
        _audit(
            db, incident, clock, "pr_prepared",
            {"pull_request_id": pr.id, "repair_attempt_id": attempt.id, "branch": pr.branch},
        )
        db.commit()

    if not settings.github_publish:
        return pr
    if not pr.github_repo or not _GITHUB_SLUG.match(pr.github_repo):
        return pr

    # The repository's base branch is protected on top of the default ones.
    if isinstance(runner, AllowListRunner):
        allow_list = AllowListRunner(runner.inner, {*runner.protected_branches, pr.base})
    else:
        allow_list = AllowListRunner(runner, {*DEFAULT_PROTECTED_BRANCHES, pr.base})
    workspace = find_workspace(settings.workdir, incident.id, pr.branch)
    try:
        if workspace is None:
            raise _PublishError("the isolated copy of the fix branch was not found")
        url = _publish(pr, workspace, allow_list, settings)
    except _PublishError as exc:
        pr.status = STATUS_PUBLISH_FAILED
        pr.error = redact_text(str(exc))[:MAX_ERROR_CHARS]
        _audit(db, incident, clock, "pr_publish_failed", {"pull_request_id": pr.id, "error": pr.error})
        db.commit()
        return pr

    now: datetime = clock.now()
    pr.status = STATUS_PUBLISHED
    pr.url = url
    pr.error = None
    pr.published_at = now
    attempt.pr_url = url
    _audit(
        db, incident, clock, "pr_published",
        {"pull_request_id": pr.id, "url": url, "branch": pr.branch, "base": pr.base},
    )
    db.commit()
    return pr
