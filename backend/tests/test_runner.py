import os
import subprocess
import sys

import pytest

import app.runner as runner_module
from app.runner import (
    AllowListRunner,
    CommandNotAllowed,
    CommandResult,
    FakeRunner,
    SubprocessRunner,
    minimal_env,
    truncate_output,
)

# The base interpreter avoids the Windows venv launcher, which runs Python as a
# grandchild process (killing the launcher alone would not stop the sleep).
_base = getattr(sys, "_base_executable", None)
PYTHON = _base if _base and os.path.isfile(_base) else sys.executable


# -- AllowListRunner ------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "merge", "x"],
        ["git", "push", "origin", "main"],
        ["rm", "-rf", "/"],
    ],
)
def test_allow_list_rejects_forbidden_commands(argv):
    inner = FakeRunner()
    runner = AllowListRunner(inner)
    with pytest.raises(CommandNotAllowed):
        runner.run(argv)
    assert inner.calls == []


def test_allow_list_allows_git_status():
    inner = FakeRunner(default=CommandResult(returncode=0, stdout="clean"))
    runner = AllowListRunner(inner)
    result = runner.run(["git", "status"], cwd="somewhere", timeout=5)
    assert result.stdout == "clean"
    assert inner.argvs == [["git", "status"]]
    assert inner.calls[0].cwd == "somewhere"
    assert inner.calls[0].timeout == 5


@pytest.mark.parametrize(
    "argv",
    [
        ["git", "clone", "--branch", "main", "--", "https://example.invalid/r.git", "dst"],
        ["git", "init", "-b", "main"],
        ["git", "checkout", "-b", "sentinel/fix-1-1"],
        ["git", "switch", "-c", "sentinel/fix-1-2"],
        ["git", "add", "-A"],
        ["git", "commit", "-m", "fix"],
        ["git", "diff", "HEAD"],
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        ["git", "config", "user.name", "Sentinel AI"],
        ["git", "branch", "--list"],
        ["git", "log", "--oneline"],
        ["git", "apply", "--check", "patch.diff"],
        ["git", "push", "-u", "origin", "sentinel/fix-1-1"],
        ["git", "push", "origin", "HEAD:refs/heads/sentinel/fix-1-1"],
        ["gh", "pr", "create", "--draft", "--base", "main", "--title", "t", "--body", "b"],
    ],
)
def test_allow_list_allows_listed_subcommands(argv):
    inner = FakeRunner()
    AllowListRunner(inner).run(argv)
    assert inner.argvs == [argv]


@pytest.mark.parametrize(
    "argv",
    [
        ["git"],
        ["git", "pull"],
        ["git", "reset", "--hard"],
        ["git", "-c", "core.sshCommand=evil", "status"],
        ["git", "push", "origin", "master"],
        ["git", "push", "origin", "HEAD:main"],
        ["git", "push", "origin", "sentinel/fix-1-1:refs/heads/main"],
        ["git", "push", "origin", "+sentinel/fix-1-1"],
        ["git", "push", "--force", "origin", "sentinel/fix-1-1"],
        ["git", "push", "origin", ":sentinel/fix-1-1"],
        ["git", "push", "origin", "feature/other"],
        ["git", "push"],
        ["git", "push", "--mirror", "origin"],
        ["git", "config", "--global", "user.name", "x"],
        ["git", "config", "core.sshCommand", "evil"],
        ["git", "config", "alias.x", "!rm -rf /"],
        ["git", "clone", "--upload-pack=evil", "url", "dst"],
        ["git", "clone", "-u", "evil", "url", "dst"],
        ["git", "clone", "-c", "core.hooksPath=evil", "url", "dst"],
        ["git", "diff", "--output=/tmp/x"],
        ["gh", "pr", "merge", "1"],
        ["gh", "repo", "delete"],
        ["/usr/bin/git", "status"],
        ["bash", "-c", "git status"],
    ],
)
def test_allow_list_rejects_dangerous_variants(argv):
    inner = FakeRunner()
    with pytest.raises(CommandNotAllowed):
        AllowListRunner(inner).run(argv)
    assert inner.calls == []


def test_allow_list_honours_custom_protected_branches():
    runner = AllowListRunner(FakeRunner(), protected_branches=["develop"])
    with pytest.raises(CommandNotAllowed):
        runner.check(["git", "push", "origin", "HEAD:develop"])
    runner.check(["git", "push", "origin", "sentinel/fix-3-1"])


def test_allow_list_rejects_shell_strings():
    with pytest.raises(CommandNotAllowed):
        AllowListRunner(FakeRunner()).run("git status")  # type: ignore[arg-type]


# -- SubprocessRunner -------------------------------------------------------------


def test_subprocess_runner_never_uses_a_shell(monkeypatch):
    seen: list[dict] = []
    real_popen = subprocess.Popen

    def spy(*args, **kwargs):
        seen.append(kwargs)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(runner_module.subprocess, "Popen", spy)
    payload = "a && echo injected; $(whoami) | more"
    result = SubprocessRunner().run(
        [PYTHON, "-c", "import sys; print(sys.argv[1])", payload], timeout=30
    )
    assert result.returncode == 0
    assert result.timed_out is False
    assert result.stdout.strip() == payload
    assert seen and all(kwargs.get("shell") is False for kwargs in seen)


def test_subprocess_runner_rejects_shell_strings():
    with pytest.raises(TypeError):
        SubprocessRunner().run("echo hi")  # type: ignore[arg-type]


def test_subprocess_runner_reports_timeout():
    result = SubprocessRunner().run([PYTHON, "-c", "import time; time.sleep(20)"], timeout=0.5)
    assert result.timed_out is True
    assert result.returncode != 0
    assert result.ok is False
    assert result.duration < 15
    assert "timed out" in result.stderr


def test_subprocess_runner_does_not_leak_environment(monkeypatch):
    monkeypatch.setenv("SENTINEL_SECRET_X", "fictitious-secret-value")
    script = (
        "import os; "
        "print(os.environ.get('SENTINEL_SECRET_X', '<absent>')); "
        "print(os.environ.get('SENTINEL_EXPLICIT', '<absent>'))"
    )
    result = SubprocessRunner().run(
        [PYTHON, "-c", script], timeout=30, env={"SENTINEL_EXPLICIT": "passed"}
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.split()
    assert lines == ["<absent>", "passed"]
    assert "fictitious-secret-value" not in result.stdout


def test_minimal_env_only_keeps_essentials(monkeypatch):
    monkeypatch.setenv("SENTINEL_SECRET_X", "nope")
    monkeypatch.setenv("SOME_UNRELATED_VAR", "nope")
    env = minimal_env({"EXTRA": "1"})
    assert "SENTINEL_SECRET_X" not in env
    assert "SOME_UNRELATED_VAR" not in env
    assert env["EXTRA"] == "1"
    if "PATH" in os.environ:
        assert env["PATH"] == os.environ["PATH"]


def test_subprocess_runner_redacts_and_truncates_output():
    runner = SubprocessRunner(max_output_chars=200)
    result = runner.run(
        [PYTHON, "-c", "print('x' * 1000); print('password=hunter2-fictitious')"], timeout=30
    )
    assert result.returncode == 0
    assert "hunter2-fictitious" not in result.stdout
    assert "[REDACTED]" in result.stdout
    assert "truncated" in result.stdout
    assert len(result.stdout) < 300


def test_subprocess_runner_reports_nonzero_exit_and_stderr():
    result = SubprocessRunner().run(
        [PYTHON, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"], timeout=30
    )
    assert result.returncode == 3
    assert result.stderr == "boom"
    assert result.timed_out is False


def test_subprocess_runner_missing_executable():
    result = SubprocessRunner().run(["definitely-not-a-real-command-sentinel"], timeout=5)
    assert result.returncode == 127
    assert result.ok is False


def test_subprocess_runner_uses_cwd(tmp_path):
    result = SubprocessRunner().run(
        [PYTHON, "-c", "import os; print(os.getcwd())"], cwd=tmp_path, timeout=30
    )
    assert os.path.samefile(result.stdout.strip(), tmp_path)


def test_truncate_output_keeps_tail():
    text = "head" + "-" * 50 + "tail"
    truncated = truncate_output(text, 10)
    assert truncated.endswith(text[-10:])
    assert "truncated" in truncated
    assert truncate_output("short", 10) == "short"


# -- FakeRunner -----------------------------------------------------------------


def test_fake_runner_records_calls_and_returns_scripted_results():
    fake = FakeRunner(results=[CommandResult(returncode=1, stderr="first")])
    fake.script(["git", "rev-parse"], CommandResult(returncode=0, stdout="abc123"))

    assert fake.run(["git", "rev-parse", "HEAD"]).stdout == "abc123"
    assert fake.run(["git", "status"], cwd="w", timeout=3, env={"A": "1"}).stderr == "first"
    assert fake.run(["git", "status"]).returncode == 0  # default

    assert fake.argvs == [["git", "rev-parse", "HEAD"], ["git", "status"], ["git", "status"]]
    assert fake.calls[1].cwd == "w"
    assert fake.calls[1].timeout == 3
    assert fake.calls[1].env == {"A": "1"}


def test_fake_runner_handler():
    fake = FakeRunner(handler=lambda argv: CommandResult(returncode=9) if "x" in argv else None)
    assert fake.run(["git", "x"]).returncode == 9
    assert fake.run(["git", "y"]).returncode == 0
