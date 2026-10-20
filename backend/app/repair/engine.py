"""Repair engine: patch, verify with the repository's tests, retry and stop safely.

``run_repair`` creates an isolated copy of the authorized repository (T13) on
a ``sentinel/fix-<incident>-<n>`` branch and, for up to
``settings.max_repair_attempts`` attempts, asks the AI provider for a
``PatchModel`` (T14) through an injection-safe prompt that carries the RCA, the
relevant files and, on retries, the redacted output of the previous attempt.

Each patch is validated; when it adds a regression test, only the test changes
are applied first and that test file is run on the pre-patch tree to record
``regression_test_failed_before`` (true, false or unknown). The full patch is
then applied and the repository's fixed ``test_command`` argv is run with a
timeout. An attempt succeeds only when the tests return 0 and, when the check
was possible, the regression test failed before the patch; the model's own
claim of success is never taken into account. A successful change is
committed on the fix branch of the copy; the source repository is never
touched.

The run stops with a recorded ``stop_reason`` on: unauthorized repository,
patch validation failure (security risk), test timeout, attempts, time or
cost budget exhausted, provider not configured and no patch available from
the provider (``unverifiable``). Every attempt is persisted
as a ``RepairAttempt`` row and audit/timeline entries ``repair_*`` are recorded.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.base import AIProvider, NoPatchAvailable, Prompt, ProviderNotConfigured
from app.ai.prompts import RepoExcerpt, build_repair_prompt
from app.clock import Clock
from app.config import Settings
from app.models import Application, AuditLog, AuthorizedRepository, Event, Incident, RepairAttempt
from app.rca.schemas import RCAReportModel
from app.repair.patch import (
    FileAction,
    PatchApplyError,
    PatchLimits,
    PatchModel,
    PatchRejected,
    _normalize,
    apply_patch,
    compute_diff,
    validate_patch,
)
from app.repair.workspace import Workspace, WorkspaceError, create_isolated_copy
from app.repo_tools import RepoAccessError, RepoReader
from app.runner import CommandResult, CommandRunner, truncate_output
from app.security.redaction import redact_text

REPAIR_ACTOR = "repair"
MAX_RELEVANT_FILES = 5
MAX_STORED_OUTPUT_CHARS = 50_000
MAX_ERROR_CHARS = 2000
GIT_TIMEOUT_SECONDS = 120.0
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}
# Keep test runs from writing bytecode into the copy.
TEST_ENV = {"PYTHONDONTWRITEBYTECODE": "1"}
# Test artifacts that must never end up in the fix commit.
EXCLUDED_ARTIFACTS = ("__pycache__/", "*.pyc", ".pytest_cache/")

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"

STOP_UNAUTHORIZED = "unauthorized_repo"
STOP_SECURITY_RISK = "security_risk"
STOP_TIMEOUT = "timeout"
STOP_ATTEMPTS_EXHAUSTED = "attempts_exhausted"
STOP_TIME_BUDGET = "time_budget_exhausted"
STOP_BUDGET = "budget_exhausted"
STOP_PROVIDER = "provider_not_configured"
STOP_UNVERIFIABLE = "unverifiable"
STOP_WORKSPACE = "workspace_error"
STOP_COMMIT = "commit_failed"

# Validation failures that indicate an attempt to escape the sandbox or to
# change protected files: the run stops instead of retrying.
SECURITY_REJECTIONS = frozenset(
    {
        "absolute_path",
        "path_traversal",
        "git_metadata",
        "ci_config",
        "secret_file",
        "protected_file",
        "symlink_escape",
        "invalid_path",
        "invalid_diff",
        "invalid_root",
    }
)

_SUMMARY = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected)\b")


@dataclass
class RepairResult:
    status: str
    stop_reason: str | None
    attempts: list[RepairAttempt]
    branch: str | None = None
    workspace_path: Path | None = None
    commit_sha: str | None = None
    regression_test_failed_before: bool | None = None
    prompts: list[Prompt] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.status == STATUS_SUCCEEDED


class _GitError(Exception):
    pass


# -- helpers --------------------------------------------------------------------


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _duration_ms(started: datetime, finished: datetime) -> int:
    return max(int((_as_utc(finished) - _as_utc(started)).total_seconds() * 1000), 0)


def _error_text(exc: BaseException) -> str:
    return redact_text(f"{type(exc).__name__}: {exc}")[:MAX_ERROR_CHARS]


def parse_test_summary(output: str) -> dict[str, int]:
    """Extract pytest-style counts (``passed``, ``failed``, ``errors``...)."""
    counts: dict[str, int] = {}
    for number, label in _SUMMARY.findall(output or ""):
        key = "errors" if label.startswith("error") else label
        counts[key] = int(number)
    return counts


def _command_output(result: CommandResult) -> str:
    text = result.stdout or ""
    if result.stderr:
        text = f"{text}\n{result.stderr}" if text else result.stderr
    return truncate_output(redact_text(text), MAX_STORED_OUTPUT_CHARS)


def _is_pytest(argv: Sequence[str]) -> bool:
    return any("pytest" in Path(arg).name.lower() or arg == "py.test" for arg in argv)


def _valid_test_command(command: object) -> bool:
    return (
        isinstance(command, list)
        and bool(command)
        and all(isinstance(item, str) and item for item in command)
    )


def _is_authorized(db: Session, repo: object, incident: Incident) -> bool:
    if not isinstance(repo, AuthorizedRepository) or repo.id is None:
        return False
    if repo.application_id != incident.application_id:
        return False
    return db.get(AuthorizedRepository, repo.id) is not None


def _rca_dict(rca_report: Any) -> dict[str, Any] | None:
    if rca_report is None:
        return None
    if isinstance(rca_report, RCAReportModel):
        return rca_report.model_dump(mode="json")
    raw = rca_report if isinstance(rca_report, dict) else getattr(rca_report, "report", None)
    if not raw:
        return None
    try:
        return RCAReportModel.model_validate(raw).model_dump(mode="json")
    except ValidationError:
        return None


def _relevant_files(
    db: Session, incident: Incident, rca: dict[str, Any] | None, reader: RepoReader
) -> list[RepoExcerpt]:
    paths: list[str] = []
    for location in (rca or {}).get("suspect_locations") or []:
        file = str(location.get("file") or "")
        if file:
            paths.extend(reader.find_relevant_files(f'File "{file}", line 1'))
    latest = db.scalars(
        select(Event)
        .where(Event.incident_id == incident.id)
        .order_by(Event.timestamp.desc(), Event.id.desc())
        .limit(1)
    ).first()
    if latest is not None:
        paths.extend(reader.find_relevant_files(latest.stack_trace or ""))

    excerpts: list[RepoExcerpt] = []
    for path in dict.fromkeys(paths):
        try:
            excerpts.append(RepoExcerpt(path=path, content=reader.read_file(path)))
        except RepoAccessError:
            continue
        if len(excerpts) >= MAX_RELEVANT_FILES:
            break
    return excerpts


def _exclude_test_artifacts(root: Path) -> None:
    git_dir = root / ".git"
    if not git_dir.is_dir():
        return
    info = git_dir / "info"
    info.mkdir(exist_ok=True)
    exclude = info / "exclude"
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    exclude.write_text(existing + "\n" + "\n".join(EXCLUDED_ARTIFACTS) + "\n", encoding="utf-8")


def _snapshot(root: Path, paths: Sequence[str]) -> dict[str, bytes | None]:
    snapshot: dict[str, bytes | None] = {}
    for rel in paths:
        target = root / rel
        snapshot[rel] = target.read_bytes() if target.is_file() else None
    return snapshot


def _restore(root: Path, snapshot: dict[str, bytes | None]) -> None:
    for rel, content in snapshot.items():
        target = root / rel
        if content is None:
            if target.is_file():
                target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)


def _git(runner: CommandRunner, args: list[str], cwd: Path) -> CommandResult:
    result = runner.run(["git", *args], cwd=str(cwd), timeout=GIT_TIMEOUT_SECONDS, env=GIT_ENV)
    if result.timed_out:
        raise _GitError(f"git {args[0]} timed out")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise _GitError(f"git {args[0]} failed ({result.returncode}): {detail}")
    return result


def _commit(runner: CommandRunner, workspace: Workspace, incident: Incident) -> str | None:
    _git(runner, ["add", "-A"], workspace.path)
    message = f"Sentinel AI: fix incident #{incident.id}"
    _git(runner, ["commit", "--no-verify", "-m", message], workspace.path)
    sha = _git(runner, ["rev-parse", "HEAD"], workspace.path).stdout.strip()
    return sha[:64] or None


# -- run --------------------------------------------------------------------------


class _RepairRun:
    def __init__(
        self,
        db: Session,
        incident: Incident,
        repo: object,
        clock: Clock,
        job_id: int | None,
    ) -> None:
        self.db = db
        self.incident = incident
        self.repo_id = repo.id if isinstance(repo, AuthorizedRepository) else None
        self.clock = clock
        self.job_id = job_id
        self.started = clock.now()
        self.attempts: list[RepairAttempt] = []
        self.prompts: list[Prompt] = []
        self.workspace: Workspace | None = None
        self.attempt_started: datetime = self.started

    def audit(self, action: str, details: dict[str, Any]) -> None:
        self.db.add(
            AuditLog(
                application_id=self.incident.application_id,
                incident_id=self.incident.id,
                actor=REPAIR_ACTOR,
                action=action,
                details=details,
                created_at=self.clock.now(),
            )
        )

    def new_attempt(self, number: int, test_command: list[str] | None) -> RepairAttempt:
        self.attempt_started = self.clock.now()
        attempt = RepairAttempt(
            incident_id=self.incident.id,
            job_id=self.job_id,
            repository_id=self.repo_id,
            attempt_number=number,
            branch_name=self.workspace.branch if self.workspace else None,
            status="running",
            test_command=test_command,
            created_at=self.attempt_started,
        )
        self.db.add(attempt)
        self.db.flush()
        self.attempts.append(attempt)
        return attempt

    def finish_attempt(
        self,
        attempt: RepairAttempt,
        status: str,
        *,
        stop_reason: str | None = None,
        error: str | None = None,
    ) -> None:
        finished = self.clock.now()
        attempt.status = status
        attempt.stop_reason = stop_reason
        if error is not None:
            attempt.error = redact_text(error)[:MAX_ERROR_CHARS]
        attempt.finished_at = finished
        attempt.duration_ms = _duration_ms(self.attempt_started, finished)
        action = "repair_attempt_succeeded" if status == STATUS_SUCCEEDED else "repair_attempt_failed"
        if status == STATUS_STOPPED:
            action = "repair_attempt_stopped"
        self.audit(
            action,
            {
                "repair_attempt_id": attempt.id,
                "attempt": attempt.attempt_number,
                "branch": attempt.branch_name,
                "returncode": attempt.returncode,
                "stop_reason": stop_reason,
                "error": attempt.error,
                "duration_ms": attempt.duration_ms,
                "cost_usd": attempt.cost_usd,
            },
        )

    def result(
        self,
        status: str,
        stop_reason: str | None,
        *,
        commit_sha: str | None = None,
        regression: bool | None = None,
    ) -> RepairResult:
        action = {
            STATUS_SUCCEEDED: "repair_succeeded",
            STATUS_FAILED: "repair_failed",
        }.get(status, "repair_stopped")
        costs = [a.cost_usd for a in self.attempts if a.cost_usd is not None]
        self.audit(
            action,
            {
                "attempts": len(self.attempts),
                "branch": self.workspace.branch if self.workspace else None,
                "stop_reason": stop_reason,
                "commit": commit_sha,
                "regression_test_failed_before": regression,
                "cost_usd": sum(costs) if costs else None,
                "duration_ms": _duration_ms(self.started, self.clock.now()),
            },
        )
        self.db.commit()
        return RepairResult(
            status=status,
            stop_reason=stop_reason,
            attempts=list(self.attempts),
            branch=self.workspace.branch if self.workspace else None,
            workspace_path=self.workspace.path if self.workspace else None,
            commit_sha=commit_sha,
            regression_test_failed_before=regression,
            prompts=list(self.prompts),
        )

    def stop(self, reason: str, error: str, test_command: list[str] | None = None) -> RepairResult:
        """Stop between attempts: record the reason on the last attempt (or a new row)."""
        if self.attempts and self.attempts[-1].status != "running":
            last = self.attempts[-1]
            last.stop_reason = reason
            last.error = redact_text(error)[:MAX_ERROR_CHARS]
            self.audit("repair_stop_recorded", {"repair_attempt_id": last.id, "stop_reason": reason})
        else:
            attempt = self.new_attempt(len(self.attempts) + 1, test_command)
            self.finish_attempt(attempt, STATUS_STOPPED, stop_reason=reason, error=error)
        status = STATUS_FAILED if reason == STOP_ATTEMPTS_EXHAUSTED else STATUS_STOPPED
        return self.result(status, reason)


def _check_regression(
    patch: PatchModel,
    regression_files: Sequence[str],
    root: Path,
    runner: CommandRunner,
    test_runner: CommandRunner,
    test_command: list[str],
    limits: PatchLimits,
    timeout: float,
) -> tuple[bool | None, CommandResult | None]:
    """Run the patch's regression tests on the pre-patch tree."""
    changes = [
        change
        for change in patch.files
        if change.action is not FileAction.DELETE and _normalize(change.path) in regression_files
    ]
    if not changes or not _is_pytest(test_command):
        return None, None
    paths = [_normalize(change.path) for change in changes]
    snapshot = _snapshot(root, paths)
    try:
        apply_patch(
            PatchModel(files=changes, explanation=""),
            root,
            runner,
            limits,
            test_command=test_command,
        )
        result = test_runner.run(
            [*test_command, *paths], cwd=str(root), timeout=timeout, env=TEST_ENV
        )
    except (PatchRejected, PatchApplyError):
        return None, None
    finally:
        _restore(root, snapshot)
    if result.timed_out:
        return None, result
    # pytest: 1 = tests failed, 2 = collection/import errors; 0 = passed.
    if result.returncode in (1, 2):
        return True, result
    if result.returncode == 0:
        return False, result
    return None, result


def run_repair(
    db: Session,
    incident: Incident,
    rca_report: Any,
    repo: AuthorizedRepository,
    provider: AIProvider | None,
    runner: CommandRunner,
    clock: Clock,
    settings: Settings,
    *,
    test_runner: CommandRunner | None = None,
    job_id: int | None = None,
) -> RepairResult:
    """Attempt a verified repair of ``incident`` and return the result.

    ``runner`` executes ``git`` (typically an ``AllowListRunner``) and
    ``test_runner`` (default: ``runner``) the repository's test command.
    """
    test_runner = test_runner or runner
    run = _RepairRun(db, incident, repo, clock, job_id)
    run.audit(
        "repair_started",
        {"repository_id": run.repo_id, "provider": getattr(provider, "name", None)},
    )

    if not _is_authorized(db, repo, incident):
        return run.stop(STOP_UNAUTHORIZED, "repository is not authorized for this application")
    test_command = list(repo.test_command) if _valid_test_command(repo.test_command) else None
    if test_command is None:
        return run.stop(STOP_UNVERIFIABLE, "the repository has no valid test command")
    if provider is None:
        return run.stop(STOP_PROVIDER, "no AI provider is configured", test_command)

    application = db.get(Application, incident.application_id)
    try:
        run.workspace = create_isolated_copy(
            repo, settings.workdir, runner, incident.id, application_id=incident.application_id
        )
    except WorkspaceError as exc:
        reason = STOP_UNAUTHORIZED if "not authorized" in str(exc) else STOP_WORKSPACE
        return run.stop(reason, _error_text(exc), test_command)
    workspace = run.workspace
    _exclude_test_artifacts(workspace.path)
    run.audit("repair_workspace_created", {"branch": workspace.branch})
    db.commit()

    reader = RepoReader(workspace.path)
    rca = _rca_dict(rca_report)
    files = _relevant_files(db, incident, rca, reader)
    file_list = reader.list_files()
    limits = PatchLimits.from_settings(settings)
    feedback: str | None = None

    for number in range(1, settings.max_repair_attempts + 1):
        elapsed = (_as_utc(clock.now()) - _as_utc(run.started)).total_seconds()
        if elapsed >= settings.repair_time_budget_seconds:
            return run.stop(STOP_TIME_BUDGET, f"time budget exhausted after {elapsed:.0f}s")
        if (
            application is not None
            and application.budget_usd is not None
            and (application.monthly_spend_usd or 0.0) >= application.budget_usd
        ):
            return run.stop(
                STOP_BUDGET,
                f"application budget of {application.budget_usd} USD is exhausted",
                test_command,
            )

        attempt = run.new_attempt(number, test_command)
        run.audit("repair_attempt_started", {"repair_attempt_id": attempt.id, "attempt": number})
        prompt = build_repair_prompt(
            incident,
            rca,
            files,
            attempt=number,
            test_command=test_command,
            file_list=file_list,
            previous_feedback=feedback,
            repo_root=str(workspace.path),
        )
        run.prompts.append(prompt)

        try:
            ai = provider.generate_structured(prompt, PatchModel)
        except ProviderNotConfigured as exc:
            run.finish_attempt(attempt, STATUS_STOPPED, stop_reason=STOP_PROVIDER, error=_error_text(exc))
            return run.result(STATUS_STOPPED, STOP_PROVIDER)
        except NoPatchAvailable as exc:
            # Nothing to verify: retrying would only repeat the same answer.
            error = f"{NoPatchAvailable.code}: {_error_text(exc)}"
            run.finish_attempt(attempt, STATUS_STOPPED, stop_reason=STOP_UNVERIFIABLE, error=error)
            return run.result(STATUS_STOPPED, STOP_UNVERIFIABLE)
        except Exception as exc:  # noqa: BLE001 - provider failures are retried
            error = _error_text(exc)
            run.finish_attempt(attempt, STATUS_FAILED, error=error)
            feedback = f"The previous output could not be used as a patch: {error}"
            db.commit()
            continue

        attempt.input_tokens = ai.usage.input_tokens
        attempt.output_tokens = ai.usage.output_tokens
        attempt.cost_usd = ai.usage.cost_usd
        if application is not None and ai.usage.cost_usd is not None:
            application.monthly_spend_usd = (application.monthly_spend_usd or 0.0) + ai.usage.cost_usd

        try:
            # Re-validate: the provider output is untrusted.
            patch = PatchModel.model_validate(ai.output.model_dump(mode="json"))
        except ValidationError as exc:
            error = _error_text(exc)
            run.finish_attempt(attempt, STATUS_FAILED, error=error)
            feedback = f"The previous output was not a valid patch: {error}"
            db.commit()
            continue
        # Stored for the report only: the explanation never decides success.
        attempt.explanation = redact_text(patch.explanation)[:MAX_ERROR_CHARS * 5]

        try:
            validated = validate_patch(patch, workspace.path, limits, test_command=test_command)
        except PatchRejected as exc:
            error = f"patch rejected ({exc.code}): {exc}"
            if exc.code in SECURITY_REJECTIONS:
                run.finish_attempt(attempt, STATUS_STOPPED, stop_reason=STOP_SECURITY_RISK, error=error)
                return run.result(STATUS_STOPPED, STOP_SECURITY_RISK)
            run.finish_attempt(attempt, STATUS_FAILED, error=error)
            feedback = error
            db.commit()
            continue

        regression, pre_result = _check_regression(
            patch,
            validated.regression_test_files,
            workspace.path,
            runner,
            test_runner,
            test_command,
            limits,
            settings.repair_test_timeout_seconds,
        )
        attempt.regression_test_failed_before = regression
        if pre_result is not None and pre_result.timed_out:
            attempt.test_output = _command_output(pre_result)
            run.finish_attempt(
                attempt, STATUS_STOPPED, stop_reason=STOP_TIMEOUT,
                error="the regression test run on the pre-patch tree timed out",
            )
            return run.result(STATUS_STOPPED, STOP_TIMEOUT)

        snapshot = _snapshot(workspace.path, validated.paths)
        try:
            apply_patch(patch, workspace.path, runner, limits, test_command=test_command)
            attempt.diff = compute_diff(workspace.path, runner)
        except (PatchRejected, PatchApplyError) as exc:
            _restore(workspace.path, snapshot)
            error = _error_text(exc)
            run.finish_attempt(attempt, STATUS_FAILED, error=error)
            feedback = f"The previous patch could not be applied: {error}"
            db.commit()
            continue

        try:
            result = test_runner.run(
                list(test_command),
                cwd=str(workspace.path),
                timeout=settings.repair_test_timeout_seconds,
                env=TEST_ENV,
            )
        except Exception as exc:  # noqa: BLE001 - e.g. a runner refusing the command
            run.finish_attempt(
                attempt, STATUS_STOPPED, stop_reason=STOP_UNVERIFIABLE, error=_error_text(exc)
            )
            return run.result(STATUS_STOPPED, STOP_UNVERIFIABLE)

        output = _command_output(result)
        attempt.test_output = output
        attempt.returncode = result.returncode
        attempt.test_summary = parse_test_summary(output)
        if result.timed_out:
            attempt.tests_passed = False
            run.finish_attempt(
                attempt, STATUS_STOPPED, stop_reason=STOP_TIMEOUT,
                error=f"the test command timed out after {settings.repair_test_timeout_seconds:g}s",
            )
            return run.result(STATUS_STOPPED, STOP_TIMEOUT)
        attempt.tests_passed = result.returncode == 0

        if result.returncode == 0 and regression is not False:
            try:
                attempt.commit_sha = _commit(runner, workspace, incident)
            except _GitError as exc:
                run.finish_attempt(
                    attempt, STATUS_STOPPED, stop_reason=STOP_COMMIT, error=_error_text(exc)
                )
                return run.result(STATUS_STOPPED, STOP_COMMIT)
            run.finish_attempt(attempt, STATUS_SUCCEEDED)
            incident.status = "fix_proposed"
            return run.result(
                STATUS_SUCCEEDED, None, commit_sha=attempt.commit_sha, regression=regression
            )

        if result.returncode == 0:
            error = "the regression test passed before the patch: it does not reproduce the bug"
            feedback = f"{error}\n\n{output}"
        else:
            error = f"the test command failed with return code {result.returncode}"
            feedback = output
        run.finish_attempt(attempt, STATUS_FAILED, error=error)
        _restore(workspace.path, snapshot)
        db.commit()

    return run.stop(
        STOP_ATTEMPTS_EXHAUSTED,
        f"no verified repair after {settings.max_repair_attempts} attempt(s)",
        test_command,
    )
