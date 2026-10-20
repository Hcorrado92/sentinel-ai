"""Injectable command runner for ``git``/``gh`` and repository test commands.

``CommandRunner`` is the only way Sentinel starts external processes.
``SubprocessRunner`` runs an argv list without a shell, with a timeout, a
minimal environment and truncated, redacted output. ``FakeRunner`` records
calls and returns scripted results for tests. ``AllowListRunner`` wraps another
runner and only lets a fixed set of ``git``/``gh`` subcommands through: merges,
pushes to protected branches and everything else raise ``CommandNotAllowed``.
"""

import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from app.security.redaction import redact_text

DEFAULT_TIMEOUT_SECONDS = 300.0
DEFAULT_MAX_OUTPUT_CHARS = 50_000
# Grace period to collect output after killing a timed-out process.
_KILL_GRACE_SECONDS = 5.0

# Variables inherited from the parent environment (when present). Everything
# else must be passed explicitly through ``env``.
INHERITED_ENV_VARS = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "HOME",
    "USERPROFILE",
    "TEMP",
    "TMP",
    "TMPDIR",
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class CommandRunner(Protocol):
    def run(
        self,
        argv: list[str],
        cwd: str | os.PathLike[str] | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run ``argv`` (never through a shell) and return its result."""
        ...


class CommandNotAllowed(Exception):
    """Raised when a command is not on the allow-list."""


def _validate_argv(argv: Sequence[str]) -> list[str]:
    if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence):
        raise TypeError("argv must be a list of strings, not a shell string")
    items = list(argv)
    if not items or not all(isinstance(item, str) for item in items):
        raise TypeError("argv must be a non-empty list of strings")
    if any("\x00" in item for item in items):
        raise ValueError("argv items must not contain NUL characters")
    return items


def minimal_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return the inherited essentials plus the explicitly passed variables."""
    env: dict[str, str] = {}
    for name in INHERITED_ENV_VARS:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    for name, value in (extra or {}).items():
        env[str(name)] = str(value)
    return env


def truncate_output(text: str, max_chars: int) -> str:
    """Keep the tail of ``text`` (where test failures usually are)."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    dropped = len(text) - max_chars
    return f"[... {dropped} characters truncated ...]\n" + text[-max_chars:]


def _decode(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")


class SubprocessRunner:
    """Runs commands with ``subprocess`` using ``shell=False``."""

    def __init__(
        self,
        default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    ) -> None:
        self.default_timeout = default_timeout
        self.max_output_chars = max_output_chars

    def _clean(self, data: bytes | str | None) -> str:
        return truncate_output(redact_text(_decode(data)), self.max_output_chars)

    def run(
        self,
        argv: list[str],
        cwd: str | os.PathLike[str] | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        items = _validate_argv(argv)
        child_env = minimal_env(env)
        # Resolve the executable against the child's PATH so the lookup does
        # not depend on the parent's environment.
        executable = shutil.which(items[0], path=child_env.get("PATH")) or items[0]
        effective_timeout = self.default_timeout if timeout is None else timeout
        popen_kwargs: dict[str, object] = {}
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True

        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                [executable, *items[1:]],
                cwd=os.fspath(cwd) if cwd is not None else None,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                **popen_kwargs,
            )
        except OSError as exc:
            return CommandResult(
                returncode=127,
                stderr=redact_text(f"failed to start {items[0]}: {exc}"),
                duration=time.monotonic() - start,
            )

        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=effective_timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill(proc)
            try:
                stdout, stderr = proc.communicate(timeout=_KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                stdout, stderr = b"", b""
        duration = time.monotonic() - start
        returncode = proc.returncode if proc.returncode is not None else -1
        stderr_text = self._clean(stderr)
        if timed_out:
            note = f"[sentinel] command timed out after {effective_timeout:g}s"
            stderr_text = f"{stderr_text}\n{note}" if stderr_text else note
        return CommandResult(
            returncode=returncode,
            stdout=self._clean(stdout),
            stderr=stderr_text,
            timed_out=timed_out,
            duration=duration,
        )


def _kill(proc: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass


@dataclass(frozen=True)
class RecordedCall:
    argv: list[str]
    cwd: str | None
    timeout: float | None
    env: dict[str, str] | None


@dataclass
class FakeRunner:
    """Test double: records calls and returns scripted results.

    Results are chosen from ``rules`` (first argv-prefix match), then from the
    ``results`` queue in order, then ``default``.
    """

    results: list[CommandResult] = field(default_factory=list)
    default: CommandResult = field(default_factory=lambda: CommandResult(returncode=0))
    handler: Callable[[list[str]], CommandResult | None] | None = None
    calls: list[RecordedCall] = field(default_factory=list)
    rules: list[tuple[list[str], CommandResult]] = field(default_factory=list)

    def script(self, prefix: Iterable[str], result: CommandResult) -> "FakeRunner":
        self.rules.append((list(prefix), result))
        return self

    def run(
        self,
        argv: list[str],
        cwd: str | os.PathLike[str] | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        items = _validate_argv(argv)
        self.calls.append(
            RecordedCall(
                argv=items,
                cwd=os.fspath(cwd) if cwd is not None else None,
                timeout=timeout,
                env=dict(env) if env is not None else None,
            )
        )
        if self.handler is not None:
            handled = self.handler(items)
            if handled is not None:
                return handled
        for prefix, result in self.rules:
            if items[: len(prefix)] == prefix:
                return result
        if self.results:
            return self.results.pop(0)
        return self.default

    @property
    def argvs(self) -> list[list[str]]:
        return [call.argv for call in self.calls]


# -- allow-list -------------------------------------------------------------

GIT_SUBCOMMANDS = frozenset(
    {
        "clone",
        "init",
        "checkout",
        "switch",
        "add",
        "commit",
        "diff",
        "status",
        "rev-parse",
        "config",
        "branch",
        "log",
        "apply",
        "push",
    }
)
GH_SUBCOMMANDS = frozenset({("pr", "create")})
DEFAULT_PROTECTED_BRANCHES = ("main", "master")
# Only branches created by Sentinel may ever be pushed.
PUSHABLE_BRANCH_PREFIX = "sentinel/"

# Options that make git execute arbitrary programs or write outside the repo.
_GIT_FORBIDDEN_OPTIONS = (
    "--upload-pack",
    "--receive-pack",
    "--exec",
    "--template",
    "--separate-git-dir",
    "--output",
    "--ext-diff",
    "--config",
)
_CLONE_FORBIDDEN_SHORT = ("-u", "-c")
_PUSH_ALLOWED_OPTIONS = frozenset(
    {
        "-u",
        "--set-upstream",
        "--porcelain",
        "-q",
        "--quiet",
        "--no-verify",
        "-v",
        "--verbose",
        "-n",
        "--dry-run",
    }
)
_CONFIG_ALLOWED_KEYS = frozenset(
    {
        "user.name",
        "user.email",
        "commit.gpgsign",
        "tag.gpgsign",
        "core.autocrlf",
        "core.safecrlf",
        "core.filemode",
        "core.longpaths",
    }
)
_CONFIG_FORBIDDEN_OPTIONS = ("--global", "--system", "--file", "-f", "--blob", "--edit", "-e")


def _executable_name(arg: str) -> str | None:
    if arg in ("git", "git.exe"):
        return "git"
    if arg in ("gh", "gh.exe"):
        return "gh"
    return None


class AllowListRunner:
    """Wraps a runner and rejects every command not on the allow-list."""

    def __init__(
        self,
        inner: CommandRunner,
        protected_branches: Iterable[str] = DEFAULT_PROTECTED_BRANCHES,
    ) -> None:
        self.inner = inner
        self.protected_branches = {b.lower() for b in protected_branches}

    def check(self, argv: Sequence[str]) -> None:
        try:
            items = _validate_argv(argv)
        except (TypeError, ValueError) as exc:
            raise CommandNotAllowed(str(exc)) from exc
        tool = _executable_name(items[0])
        if tool == "git":
            self._check_git(items)
        elif tool == "gh":
            self._check_gh(items)
        else:
            raise CommandNotAllowed(f"executable not allowed: {items[0]}")

    def _check_git(self, items: list[str]) -> None:
        if len(items) < 2:
            raise CommandNotAllowed("git requires a subcommand")
        sub = items[1]
        args = items[2:]
        if sub == "merge":
            raise CommandNotAllowed("git merge is never allowed")
        if sub not in GIT_SUBCOMMANDS:
            raise CommandNotAllowed(f"git subcommand not allowed: {sub}")
        for arg in args:
            if arg == "--":
                break
            if any(arg == opt or arg.startswith(opt + "=") for opt in _GIT_FORBIDDEN_OPTIONS):
                raise CommandNotAllowed(f"git option not allowed: {arg}")
            if sub == "clone" and any(
                arg == opt or (arg.startswith(opt) and not arg.startswith("--"))
                for opt in _CLONE_FORBIDDEN_SHORT
            ):
                raise CommandNotAllowed(f"git clone option not allowed: {arg}")
        if sub == "push":
            self._check_push(args)
        elif sub == "config":
            self._check_config(args)

    def _check_push(self, args: list[str]) -> None:
        positional: list[str] = []
        for arg in args:
            if arg.startswith("-"):
                if arg not in _PUSH_ALLOWED_OPTIONS:
                    raise CommandNotAllowed(f"git push option not allowed: {arg}")
            else:
                positional.append(arg)
        if len(positional) < 2:
            raise CommandNotAllowed("git push requires an explicit remote and branch")
        for refspec in positional[1:]:
            if refspec.startswith("+"):
                raise CommandNotAllowed("force pushes are not allowed")
            source, _, destination = refspec.partition(":")
            if not source:
                raise CommandNotAllowed("deleting remote branches is not allowed")
            destination = destination or source
            if destination.startswith("refs/heads/"):
                destination = destination[len("refs/heads/") :]
            if destination.lower() in self.protected_branches or destination == "HEAD":
                raise CommandNotAllowed(f"push to the base branch is not allowed: {destination}")
            if not destination.startswith(PUSHABLE_BRANCH_PREFIX):
                raise CommandNotAllowed(
                    f"only {PUSHABLE_BRANCH_PREFIX}* branches may be pushed: {destination}"
                )

    @staticmethod
    def _check_config(args: list[str]) -> None:
        keys: list[str] = []
        for arg in args:
            if any(arg == opt or arg.startswith(opt + "=") for opt in _CONFIG_FORBIDDEN_OPTIONS):
                raise CommandNotAllowed(f"git config option not allowed: {arg}")
            if not arg.startswith("-"):
                keys.append(arg)
        if not keys or keys[0].lower() not in _CONFIG_ALLOWED_KEYS:
            raise CommandNotAllowed(f"git config key not allowed: {keys[0] if keys else ''}")

    @staticmethod
    def _check_gh(items: list[str]) -> None:
        if tuple(items[1:3]) not in GH_SUBCOMMANDS:
            raise CommandNotAllowed(f"gh command not allowed: {' '.join(items[1:3])}")

    def run(
        self,
        argv: list[str],
        cwd: str | os.PathLike[str] | None = None,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        self.check(argv)
        return self.inner.run(argv, cwd=cwd, timeout=timeout, env=env)
