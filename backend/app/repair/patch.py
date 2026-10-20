"""Patch models, validation and application for repair attempts.

A ``PatchModel`` holds structured file changes (create/modify/delete with the
full new content) and/or a unified diff, plus an explanation. ``validate_patch``
must accept a patch before it touches the isolated copy: it rejects absolute
paths, traversal, symlinks, ``.git/``, CI configuration, secret-like files,
the repository's protected test-command configuration and patches over the
configured file/byte limits, each with a specific reason. ``apply_patch``
writes the changes (unified diffs go through ``git apply`` via the runner) and
``compute_diff`` returns the redacted ``git diff`` of the working copy.
"""

import os
import re
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath, PureWindowsPath

from pydantic import BaseModel, Field, model_validator

from app.config import Settings
from app.runner import CommandResult, CommandRunner
from app.security.redaction import redact_text

GIT_TIMEOUT_SECONDS = 120.0
GIT_ENV = {"GIT_TERMINAL_PROMPT": "0"}

DEFAULT_MAX_FILES = 20
DEFAULT_MAX_BYTES = 200 * 1024

# Files that configure how the repository's fixed test command runs. A patch
# must never change them (e.g. to skip or deselect the failing tests).
DEFAULT_PROTECTED_FILES = (
    "pyproject.toml",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "conftest.py",
    "noxfile.py",
    "makefile",
    "package.json",
)
_CI_DIRS = frozenset({".github", ".circleci"})
_CI_FILES = frozenset({".gitlab-ci.yml", "azure-pipelines.yml"})
_SECRET_SUFFIXES = (".pem", ".key")
_TEST_DIRS = frozenset({"tests", "test"})
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
_DIFF_GIT_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")
# Symlinks (120000) and submodules (160000) are never allowed in a diff.
_FORBIDDEN_MODE = re.compile(r"^(?:new file mode|deleted file mode|old mode|new mode) 1[26]0000")
# Windows 8.3 short names (e.g. GIT~1) could alias protected directories.
_SHORT_NAME = re.compile(r"~\d")


class FileAction(str, Enum):
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"


class FileChange(BaseModel):
    path: str = Field(min_length=1, max_length=1024)
    action: FileAction
    content: str | None = None

    @model_validator(mode="after")
    def _check_content(self) -> "FileChange":
        if self.action is FileAction.DELETE:
            if self.content is not None:
                raise ValueError("delete changes must not include content")
        elif self.content is None:
            raise ValueError(f"{self.action.value} changes require content")
        return self


class PatchModel(BaseModel):
    files: list[FileChange] = Field(default_factory=list)
    unified_diff: str | None = None
    explanation: str = ""

    @model_validator(mode="after")
    def _check_not_empty(self) -> "PatchModel":
        if not self.files and not (self.unified_diff and self.unified_diff.strip()):
            raise ValueError("a patch needs file changes or a unified diff")
        return self


@dataclass(frozen=True)
class PatchLimits:
    max_files: int = DEFAULT_MAX_FILES
    max_bytes: int = DEFAULT_MAX_BYTES
    protected_files: tuple[str, ...] = DEFAULT_PROTECTED_FILES

    @classmethod
    def from_settings(cls, settings: Settings) -> "PatchLimits":
        return cls(max_files=settings.max_patch_files, max_bytes=settings.max_patch_bytes)


class PatchRejected(Exception):
    """Raised when a patch fails validation. ``code`` names the reason."""

    def __init__(self, code: str, message: str, path: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


class PatchApplyError(Exception):
    """Raised when a validated patch cannot be applied to the working copy."""


@dataclass(frozen=True)
class ValidatedPatch:
    """Summary of an accepted patch."""

    paths: tuple[str, ...]
    total_bytes: int
    regression_test_files: tuple[str, ...] = field(default=())

    @property
    def touches_regression_test(self) -> bool:
        """Whether the patch adds or changes a regression test file."""
        return bool(self.regression_test_files)


def is_test_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1].lower() if parts else ""
    return (
        any(part.lower() in _TEST_DIRS for part in parts[:-1])
        or (name.startswith("test_") and name.endswith(".py"))
        or name.endswith("_test.py")
    )


# -- path checks --------------------------------------------------------------


def _normalize(raw: str) -> str:
    """Return the repository-relative POSIX path or raise ``PatchRejected``."""
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise PatchRejected("invalid_path", "empty or invalid path", raw)
    path = raw.replace("\\", "/")
    if (
        path.startswith("/")
        or PureWindowsPath(raw).is_absolute()
        or PureWindowsPath(raw).drive
        or re.match(r"^[A-Za-z]:", path)
    ):
        raise PatchRejected("absolute_path", f"absolute paths are not allowed: {raw}", raw)
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise PatchRejected("path_traversal", f"path traversal is not allowed: {raw}", raw)
    if not parts:
        raise PatchRejected("invalid_path", f"path does not name a file: {raw}", raw)
    for part in parts:
        # Windows ignores trailing dots/spaces, so ".git." would alias ".git".
        if ":" in part or not part.rstrip(". ") or _SHORT_NAME.search(part):
            raise PatchRejected("invalid_path", f"invalid path component in: {raw}", raw)
    return "/".join(parts)


def _protected_set(
    root: Path, limits: PatchLimits, test_command: Sequence[str] | None
) -> set[str]:
    protected = {name.lower() for name in limits.protected_files}
    for arg in test_command or ():
        if not isinstance(arg, str) or not arg or arg.startswith("-"):
            continue
        try:
            rel = _normalize(arg)
        except PatchRejected:
            continue
        if (root / rel).is_file():
            protected.add(rel.lower())
    return protected


def _check_path(raw: str, root: Path, protected: set[str]) -> str:
    rel = _normalize(raw)
    parts = [part.rstrip(". ").lower() for part in rel.split("/")]
    name = parts[-1]
    if ".git" in parts:
        raise PatchRejected("git_metadata", f"changes under .git/ are not allowed: {raw}", raw)
    if _CI_DIRS.intersection(parts) or name in _CI_FILES:
        raise PatchRejected("ci_config", f"CI configuration must not be changed: {raw}", raw)
    if (
        any(part.startswith(".env") for part in parts)
        or name.endswith(_SECRET_SUFFIXES)
        or name.startswith("id_rsa")
    ):
        raise PatchRejected("secret_file", f"secret-like files must not be changed: {raw}", raw)
    if "/".join(parts) in protected:
        raise PatchRejected(
            "protected_file", f"the test-command configuration is protected: {raw}", raw
        )

    current = root
    for part in rel.split("/"):
        current = current / part
        if current.is_symlink():
            raise PatchRejected("symlink_escape", f"symlinks are not allowed in paths: {raw}", raw)
    resolved = (root / rel).resolve()
    if resolved == root or root not in resolved.parents:
        raise PatchRejected("symlink_escape", f"path resolves outside the repository: {raw}", raw)
    return rel


# -- unified diff parsing -----------------------------------------------------


def _strip_prefix(path: str, prefix: str, line: str) -> str | None:
    path = path.split("\t", 1)[0].rstrip()
    if path == "/dev/null":
        return None
    if path.startswith('"'):
        raise PatchRejected("invalid_diff", f"quoted paths are not supported: {line}")
    if not path.startswith(prefix):
        raise PatchRejected("invalid_diff", f"diff paths must start with {prefix}: {line}")
    return path[len(prefix) :]


def diff_paths(diff: str) -> list[str]:
    """Return every path a unified diff touches (raises ``PatchRejected``)."""
    paths: list[str] = []
    old_left = new_left = 0
    for raw_line in diff.split("\n"):
        line = raw_line.rstrip("\r")
        if old_left > 0 or new_left > 0:
            if line.startswith("\\"):
                continue
            marker = line[:1]
            if marker in (" ", ""):
                old_left -= 1
                new_left -= 1
            elif marker == "-":
                old_left -= 1
            elif marker == "+":
                new_left -= 1
            else:
                raise PatchRejected("invalid_diff", "hunk line counts do not match")
            if old_left < 0 or new_left < 0:
                raise PatchRejected("invalid_diff", "hunk line counts do not match")
            continue

        hunk = _HUNK_HEADER.match(line)
        if hunk:
            old_left = int(hunk.group(1)) if hunk.group(1) is not None else 1
            new_left = int(hunk.group(2)) if hunk.group(2) is not None else 1
            continue
        if line.startswith("GIT binary patch") or line.startswith("Binary files "):
            raise PatchRejected("invalid_diff", "binary patches are not allowed")
        if _FORBIDDEN_MODE.match(line):
            raise PatchRejected("invalid_diff", "symlink or submodule changes are not allowed")
        if line.startswith("diff --git "):
            header = _DIFF_GIT_HEADER.match(line)
            if not header:
                raise PatchRejected("invalid_diff", f"unsupported diff header: {line}")
            paths.extend([header.group(1), header.group(2)])
        elif line.startswith("--- "):
            path = _strip_prefix(line[4:], "a/", line)
            if path is not None:
                paths.append(path)
        elif line.startswith("+++ "):
            path = _strip_prefix(line[4:], "b/", line)
            if path is not None:
                paths.append(path)
        else:
            for keyword in ("rename from ", "rename to ", "copy from ", "copy to "):
                if line.startswith(keyword):
                    paths.append(line[len(keyword) :])
    if old_left > 0 or new_left > 0:
        raise PatchRejected("invalid_diff", "the diff ends in the middle of a hunk")
    if not paths:
        raise PatchRejected("invalid_diff", "the unified diff does not name any file")
    return paths


# -- validation -----------------------------------------------------------------


def validate_patch(
    patch: PatchModel,
    root: str | os.PathLike[str],
    limits: PatchLimits | None = None,
    *,
    test_command: Sequence[str] | None = None,
) -> ValidatedPatch:
    """Validate ``patch`` against the isolated copy at ``root``."""
    limits = limits or PatchLimits()
    root_path = Path(root).resolve()
    if not root_path.is_dir():
        raise PatchRejected("invalid_root", f"repository root does not exist: {root}")

    total_bytes = sum(len((change.content or "").encode("utf-8")) for change in patch.files)
    if patch.unified_diff:
        total_bytes += len(patch.unified_diff.encode("utf-8"))
    if total_bytes > limits.max_bytes:
        raise PatchRejected(
            "too_large", f"patch is {total_bytes} bytes; the limit is {limits.max_bytes}"
        )

    raw_paths = [change.path for change in patch.files]
    if patch.unified_diff and patch.unified_diff.strip():
        raw_paths.extend(diff_paths(patch.unified_diff))
    unique: dict[str, None] = {}
    for raw in raw_paths:
        unique[_normalize(raw)] = None
    if len(unique) > limits.max_files:
        raise PatchRejected(
            "too_many_files", f"patch touches {len(unique)} files; the limit is {limits.max_files}"
        )

    protected = _protected_set(root_path, limits, test_command)
    for raw in raw_paths:
        _check_path(raw, root_path, protected)

    seen: set[str] = set()
    tests: dict[str, None] = {}
    for change in patch.files:
        rel = _normalize(change.path)
        if rel in seen:
            raise PatchRejected("duplicate_path", f"path changed more than once: {rel}", rel)
        seen.add(rel)
        target = root_path / rel
        if target.is_dir():
            raise PatchRejected("invalid_path", f"path is a directory: {rel}", rel)
        if change.action is FileAction.CREATE and target.exists():
            raise PatchRejected("file_exists", f"file to create already exists: {rel}", rel)
        if change.action is not FileAction.CREATE and not target.is_file():
            raise PatchRejected(
                "file_missing", f"file to {change.action.value} is missing: {rel}", rel
            )
        if change.action is not FileAction.DELETE and is_test_path(rel):
            tests[rel] = None
    if patch.unified_diff:
        for raw in diff_paths(patch.unified_diff):
            rel = _normalize(raw)
            if is_test_path(rel):
                tests[rel] = None

    return ValidatedPatch(
        paths=tuple(unique), total_bytes=total_bytes, regression_test_files=tuple(tests)
    )


# -- application ----------------------------------------------------------------


def _git(runner: CommandRunner, args: list[str], cwd: Path) -> CommandResult:
    result = runner.run(["git", *args], cwd=str(cwd), timeout=GIT_TIMEOUT_SECONDS, env=GIT_ENV)
    if result.timed_out:
        raise PatchApplyError(f"git {args[0]} timed out")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise PatchApplyError(f"git {args[0]} failed ({result.returncode}): {detail}")
    return result


def apply_patch(
    patch: PatchModel,
    root: str | os.PathLike[str],
    runner: CommandRunner,
    limits: PatchLimits | None = None,
    *,
    test_command: Sequence[str] | None = None,
) -> ValidatedPatch:
    """Validate and write ``patch`` into the isolated copy at ``root``."""
    validated = validate_patch(patch, root, limits, test_command=test_command)
    root_path = Path(root).resolve()

    diff_dir: str | None = None
    try:
        diff_file: Path | None = None
        if patch.unified_diff and patch.unified_diff.strip():
            # The diff file lives outside the repository so it is never committed.
            diff_dir = tempfile.mkdtemp(prefix="sentinel-patch-")
            diff_file = Path(diff_dir) / "change.diff"
            text = patch.unified_diff
            if not text.endswith("\n"):
                text += "\n"
            diff_file.write_bytes(text.encode("utf-8"))
            _git(runner, ["apply", "--check", "-p1", str(diff_file)], root_path)

        for change in patch.files:
            target = root_path / _normalize(change.path)
            if change.action is FileAction.DELETE:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes((change.content or "").encode("utf-8"))

        if diff_file is not None:
            _git(runner, ["apply", "-p1", str(diff_file)], root_path)
    finally:
        if diff_dir is not None:
            shutil.rmtree(diff_dir, ignore_errors=True)
    return validated


def compute_diff(root: str | os.PathLike[str], runner: CommandRunner) -> str:
    """Return the redacted ``git diff`` of the working copy (new files included)."""
    root_path = Path(root).resolve()
    _git(runner, ["add", "-A"], root_path)
    result = _git(
        runner, ["diff", "--cached", "--no-color", "--no-ext-diff", "--no-renames"], root_path
    )
    return redact_text(result.stdout)
