import os
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.repair.patch import (
    FileAction,
    FileChange,
    PatchApplyError,
    PatchLimits,
    PatchModel,
    PatchRejected,
    apply_patch,
    compute_diff,
    diff_paths,
    validate_patch,
)
from app.runner import AllowListRunner, CommandResult, FakeRunner, SubprocessRunner

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git binary not available")

GIT = AllowListRunner(SubprocessRunner(default_timeout=60))
PRICING = "def discount(total):\n    return total * 0.1\n"


def git(cwd: Path, *args: str) -> str:
    result = GIT.run(["git", *args], cwd=cwd, timeout=60, env={"GIT_TERMINAL_PROMPT": "0"})
    assert result.returncode == 0, result.stderr
    return result.stdout


def read(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    write(repo / "shop" / "pricing.py", PRICING)
    write(repo / "pyproject.toml", "[tool.pytest.ini_options]\n")
    write(repo / "scripts" / "run_tests.py", "print('tests')\n")
    return repo


@pytest.fixture
def git_repo(root: Path) -> Path:
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test User")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.autocrlf", "false")
    git(root, "add", "-A")
    git(root, "commit", "--no-verify", "-m", "initial")
    return root


def create(path: str, content: str = "x = 1\n") -> PatchModel:
    return PatchModel(
        files=[FileChange(path=path, action=FileAction.CREATE, content=content)],
        explanation="fictitious change",
    )


def rejection(patch: PatchModel, root: Path, **kwargs) -> PatchRejected:
    with pytest.raises(PatchRejected) as info:
        validate_patch(patch, root, **kwargs)
    return info.value


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("../x.py", "path_traversal"),
        ("shop/../../x.py", "path_traversal"),
        ("/etc/passwd", "absolute_path"),
        ("C:\\Windows\\win.ini", "absolute_path"),
        (".git/config", "git_metadata"),
        ("sub/.GIT/hooks/pre-commit", "git_metadata"),
        (".github/workflows/ci.yml", "ci_config"),
        (".gitlab-ci.yml", "ci_config"),
        ("azure-pipelines.yml", "ci_config"),
        (".circleci/config.yml", "ci_config"),
        (".env", "secret_file"),
        (".env.production", "secret_file"),
        ("certs/server.pem", "secret_file"),
        ("certs/server.key", "secret_file"),
        ("home/id_rsa.pub", "secret_file"),
        ("pyproject.toml", "protected_file"),
        ("pytest.ini", "protected_file"),
        ("file.py:stream", "invalid_path"),
    ],
)
def test_validate_patch_rejects_unsafe_paths_with_specific_reason(root, path, code):
    error = rejection(create(path), root)
    assert error.code == code
    assert path in str(error)


def test_unsafe_paths_in_unified_diff_are_rejected(root):
    diff = (
        "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n"
        "--- a/.github/workflows/ci.yml\n"
        "+++ b/.github/workflows/ci.yml\n"
        "@@ -1 +1 @@\n"
        "-on: push\n"
        "+on: never\n"
    )
    assert rejection(PatchModel(unified_diff=diff), root).code == "ci_config"

    traversal = "--- a/../outside.py\n+++ b/../outside.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert rejection(PatchModel(unified_diff=traversal), root).code == "path_traversal"

    unprefixed = "--- x/pyproject.toml\n+++ x/pyproject.toml\n@@ -1 +1 @@\n-a\n+b\n"
    assert rejection(PatchModel(unified_diff=unprefixed), root).code == "invalid_diff"

    symlink = (
        "diff --git a/link b/link\nnew file mode 120000\n--- /dev/null\n+++ b/link\n"
        "@@ -0,0 +1 @@\n+/etc/passwd\n"
    )
    assert rejection(PatchModel(unified_diff=symlink), root).code == "invalid_diff"


def test_test_command_files_are_protected(root):
    command = ["python", "scripts/run_tests.py"]
    modify = PatchModel(
        files=[FileChange(path="scripts/run_tests.py", action=FileAction.MODIFY, content="")]
    )
    assert rejection(modify, root, test_command=command).code == "protected_file"
    # Without the test command the script is an ordinary file.
    assert validate_patch(modify, root).paths == ("scripts/run_tests.py",)


def test_symlink_escape_is_rejected(root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, root / "linked", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not supported here")
    assert rejection(create("linked/evil.py"), root).code == "symlink_escape"


def test_patch_over_max_files_is_rejected(root):
    files = [
        FileChange(path=f"shop/new_{i}.py", action=FileAction.CREATE, content="x = 1\n")
        for i in range(4)
    ]
    patch = PatchModel(files=files)
    error = rejection(patch, root, limits=PatchLimits(max_files=3))
    assert error.code == "too_many_files"
    assert len(validate_patch(patch, root, PatchLimits(max_files=4)).paths) == 4


def test_patch_over_max_bytes_is_rejected(root):
    patch = create("shop/big.py", "x" * 101)
    assert rejection(patch, root, limits=PatchLimits(max_bytes=100)).code == "too_large"
    assert validate_patch(patch, root, PatchLimits(max_bytes=101)).total_bytes == 101


def test_limits_from_settings():
    limits = PatchLimits.from_settings(Settings(max_patch_files=2, max_patch_bytes=50))
    assert (limits.max_files, limits.max_bytes) == (2, 50)


def test_action_consistency_is_checked(root):
    assert rejection(create("shop/pricing.py"), root).code == "file_exists"
    missing = PatchModel(files=[FileChange(path="shop/none.py", action=FileAction.DELETE)])
    assert rejection(missing, root).code == "file_missing"
    with pytest.raises(ValidationError):
        FileChange(path="a.py", action=FileAction.MODIFY)
    with pytest.raises(ValidationError):
        PatchModel(files=[], explanation="nothing")


def test_regression_test_files_are_recorded(root):
    patch = PatchModel(
        files=[
            FileChange(path="shop/pricing.py", action=FileAction.MODIFY, content=PRICING),
            FileChange(path="tests/test_pricing.py", action=FileAction.CREATE, content="\n"),
        ]
    )
    result = validate_patch(patch, root)
    assert result.touches_regression_test
    assert result.regression_test_files == ("tests/test_pricing.py",)

    plain = validate_patch(create("shop/helpers.py"), root)
    assert not plain.touches_regression_test


def test_diff_paths_ignores_hunk_lines_that_look_like_headers():
    diff = (
        "--- a/notes.sql\n"
        "+++ b/notes.sql\n"
        "@@ -1,2 +1,1 @@\n"
        "--- a/.git/config\n"
        " select 1;\n"
    )
    assert diff_paths(diff) == ["notes.sql", "notes.sql"]


@needs_git
def test_structured_patch_is_applied_and_diff_contains_change(git_repo):
    patch = PatchModel(
        files=[
            FileChange(
                path="shop/pricing.py",
                action=FileAction.MODIFY,
                content="def discount(total):\n    return total * 0.2\n",
            ),
            FileChange(
                path="tests/test_pricing.py",
                action=FileAction.CREATE,
                content="def test_discount():\n    assert True\n",
            ),
        ],
        explanation="Use the correct discount rate.",
    )

    result = apply_patch(patch, git_repo, GIT)

    assert read(git_repo / "shop" / "pricing.py").endswith("return total * 0.2\n")
    assert (git_repo / "tests" / "test_pricing.py").is_file()
    assert result.touches_regression_test

    diff = compute_diff(git_repo, GIT).replace("\r\n", "\n")
    assert "diff --git a/shop/pricing.py b/shop/pricing.py" in diff
    assert "-    return total * 0.1" in diff
    assert "+    return total * 0.2" in diff
    assert "+++ b/tests/test_pricing.py" in diff


@needs_git
def test_unified_diff_patch_is_applied_with_git_apply(git_repo):
    diff = (
        "diff --git a/shop/pricing.py b/shop/pricing.py\n"
        "--- a/shop/pricing.py\n"
        "+++ b/shop/pricing.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def discount(total):\n"
        "-    return total * 0.1\n"
        "+    return total * 0.15\n"
        "diff --git a/tests/test_discount.py b/tests/test_discount.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/tests/test_discount.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+def test_discount():\n"
        "+    assert True\n"
    )
    calls: list[list[str]] = []

    class Recording:
        def run(self, argv, cwd=None, timeout=None, env=None):
            calls.append(list(argv))
            return GIT.run(argv, cwd=cwd, timeout=timeout, env=env)

    result = apply_patch(PatchModel(unified_diff=diff, explanation="fix"), git_repo, Recording())

    assert read(git_repo / "shop" / "pricing.py") == (
        "def discount(total):\n    return total * 0.15\n"
    )
    assert read(git_repo / "tests" / "test_discount.py") == (
        "def test_discount():\n    assert True\n"
    )
    assert [argv[:2] for argv in calls] == [["git", "apply"], ["git", "apply"]]
    assert "--check" in calls[0]
    assert result.regression_test_files == ("tests/test_discount.py",)
    assert "+    return total * 0.15" in compute_diff(git_repo, GIT).replace("\r\n", "\n")


@needs_git
def test_unified_diff_that_does_not_apply_raises_and_leaves_files(git_repo):
    diff = (
        "--- a/shop/pricing.py\n"
        "+++ b/shop/pricing.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def discount(total):\n"
        "-    return total * 0.9\n"
        "+    return total * 0.15\n"
    )
    patch = PatchModel(
        files=[FileChange(path="shop/extra.py", action=FileAction.CREATE, content="y = 2\n")],
        unified_diff=diff,
    )
    with pytest.raises(PatchApplyError):
        apply_patch(patch, git_repo, GIT)
    assert read(git_repo / "shop" / "pricing.py") == PRICING
    assert not (git_repo / "shop" / "extra.py").exists()


def test_rejected_patch_never_runs_git(root):
    fake = FakeRunner()
    with pytest.raises(PatchRejected):
        apply_patch(create(".git/config"), root, fake)
    assert fake.calls == []


def test_compute_diff_is_redacted(root):
    fake = FakeRunner().script(
        ["git", "diff"],
        CommandResult(returncode=0, stdout="+API_KEY = 'password=hunter2-fictitious'\n"),
    )
    diff = compute_diff(root, fake)
    assert "hunter2-fictitious" not in diff
    assert "[REDACTED]" in diff
    assert fake.argvs[0] == ["git", "add", "-A"]
