"""Isolated git workspaces for repair attempts.

``create_isolated_copy`` never touches the authorized repository itself: it
either clones ``clone_url`` or copies ``local_path`` (without ``.git``, caches,
virtualenvs, ``.env`` files or symlinks) and runs ``git init`` plus an initial
commit. The copy lives in a unique directory under the configured workdir and
is switched to a new branch ``sentinel/fix-<incident_id>-<n>``, where ``n``
increments for every attempt series of the incident. All git calls go through
the injectable ``CommandRunner``.
"""

import os
import re
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.models import AuthorizedRepository
from app.runner import CommandResult, CommandRunner

BRANCH_PREFIX = "sentinel/fix-"
GIT_TIMEOUT_SECONDS = 120.0
# Never let git block waiting for credentials.
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}
GIT_USER_NAME = "Sentinel AI"
GIT_USER_EMAIL = "sentinel-ai@example.invalid"
_SERIES_DIR = ".sentinel-series"

_IGNORED_NAMES = frozenset(
    {
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "node_modules",
        ".env",
    }
)
_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]*$")


class WorkspaceError(Exception):
    """Raised when an isolated copy cannot (or must not) be created."""


@dataclass(frozen=True)
class Workspace:
    path: Path
    branch: str
    base_branch: str
    incident_id: int
    series: int
    repository_id: int


def branch_name(incident_id: int, series: int) -> str:
    return f"{BRANCH_PREFIX}{incident_id}-{series}"


def resolve_target_dir(workdir: str | os.PathLike[str], name: str) -> Path:
    """Resolve ``workdir/name`` and refuse anything outside the workdir."""
    root = Path(workdir).resolve()
    target = (root / name).resolve()
    if target == root or root not in target.parents:
        raise WorkspaceError(f"target directory is outside the workdir: {name}")
    return target


def next_series(workdir: str | os.PathLike[str], incident_id: int) -> int:
    """Return (and persist) the next attempt series number for an incident."""
    counters = Path(workdir).resolve() / _SERIES_DIR
    counters.mkdir(parents=True, exist_ok=True)
    counter = counters / f"incident-{incident_id}"
    try:
        current = int(counter.read_text(encoding="utf-8").strip() or "0")
    except (FileNotFoundError, ValueError):
        current = 0
    series = current + 1
    counter.write_text(str(series), encoding="utf-8")
    return series


def _ensure_authorized(repo: object, application_id: int | None) -> AuthorizedRepository:
    if not isinstance(repo, AuthorizedRepository) or repo.id is None:
        raise WorkspaceError("repository is not authorized")
    if application_id is not None and repo.application_id != application_id:
        raise WorkspaceError("repository is not authorized for this application")
    if not repo.clone_url and not repo.local_path:
        raise WorkspaceError("authorized repository has no clone URL or local path")
    return repo


def _validate_ref(name: str) -> str:
    if not _SAFE_REF.match(name) or ".." in name or name.endswith((".lock", "/")):
        raise WorkspaceError(f"invalid branch name: {name!r}")
    return name


def _slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name or "").strip("-").lower()
    return slug[:40] or "repo"


def _git(runner: CommandRunner, args: list[str], cwd: Path) -> CommandResult:
    argv = ["git", *args]
    result = runner.run(argv, cwd=str(cwd), timeout=GIT_TIMEOUT_SECONDS, env=GIT_ENV)
    if result.timed_out:
        raise WorkspaceError(f"git {args[0]} timed out")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise WorkspaceError(f"git {args[0]} failed ({result.returncode}): {detail}")
    return result


def _ignore(directory: str, names: list[str]) -> set[str]:
    ignored: set[str] = set()
    for name in names:
        if (
            name in _IGNORED_NAMES
            or name.startswith(".env.")
            or name.endswith((".pyc", ".pyo"))
            or os.path.islink(os.path.join(directory, name))
        ):
            ignored.add(name)
    return ignored


def _local_source(repo: AuthorizedRepository, root: Path) -> Path:
    source = Path(repo.local_path or "").resolve()
    if not source.is_dir():
        raise WorkspaceError(f"repository path does not exist: {repo.local_path}")
    if root == source or source in root.parents:
        raise WorkspaceError("the workdir must not be inside the source repository")
    return source


def _clone(
    repo: AuthorizedRepository, base: str, root: Path, target: Path, runner: CommandRunner
) -> None:
    url = (repo.clone_url or "").strip()
    if not url or url.startswith("-") or "::" in url:
        raise WorkspaceError("invalid clone URL")
    _git(runner, ["clone", "--branch", base, "--no-hardlinks", "--", url, str(target)], root)


def _configure(runner: CommandRunner, target: Path) -> None:
    _git(runner, ["config", "user.name", GIT_USER_NAME], target)
    _git(runner, ["config", "user.email", GIT_USER_EMAIL], target)
    _git(runner, ["config", "commit.gpgsign", "false"], target)


def _on_rm_error(func, path, _exc) -> None:  # type: ignore[no-untyped-def]
    # git marks object files read-only, which breaks rmtree on Windows.
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def remove_workspace(path: str | os.PathLike[str]) -> None:
    shutil.rmtree(path, onexc=_on_rm_error)


def create_isolated_copy(
    repo: AuthorizedRepository,
    workdir: str | os.PathLike[str],
    runner: CommandRunner,
    incident_id: int,
    *,
    application_id: int | None = None,
) -> Workspace:
    """Create an isolated copy of ``repo`` on a new ``sentinel/fix-*`` branch."""
    repo = _ensure_authorized(repo, application_id)
    if not isinstance(incident_id, int) or isinstance(incident_id, bool) or incident_id < 1:
        raise WorkspaceError("incident_id must be a positive integer")
    base = _validate_ref(repo.base_branch or "main")

    root = Path(workdir).resolve()
    source = None if repo.clone_url else _local_source(repo, root)
    root.mkdir(parents=True, exist_ok=True)
    series = next_series(root, incident_id)
    branch = _validate_ref(branch_name(incident_id, series))
    name = f"{_slug(repo.name)}-incident-{incident_id}-{series}-{uuid.uuid4().hex[:8]}"
    target = resolve_target_dir(root, name)
    if target.exists():
        raise WorkspaceError(f"target directory already exists: {target}")

    try:
        if source is None:
            _clone(repo, base, root, target, runner)
            _configure(runner, target)
        else:
            shutil.copytree(source, target, symlinks=False, ignore=_ignore)
            _git(runner, ["init", "-b", base], target)
            _configure(runner, target)
            _git(runner, ["add", "-A"], target)
            _git(
                runner,
                ["commit", "--no-verify", "--allow-empty", "-m", "Sentinel AI: initial snapshot"],
                target,
            )
        _git(runner, ["checkout", "-b", branch], target)
    except Exception:
        if target.exists():
            remove_workspace(target)
        raise

    return Workspace(
        path=target,
        branch=branch,
        base_branch=base,
        incident_id=incident_id,
        series=series,
        repository_id=repo.id,
    )
