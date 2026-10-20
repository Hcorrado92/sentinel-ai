import shutil
from pathlib import Path

import pytest

from app.models import AuthorizedRepository
from app.repair.workspace import (
    WorkspaceError,
    branch_name,
    create_isolated_copy,
    resolve_target_dir,
)
from app.runner import AllowListRunner, CommandResult, FakeRunner, SubprocessRunner

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git binary not available")

GIT = AllowListRunner(SubprocessRunner(default_timeout=60))


def git(cwd: Path, *args: str) -> str:
    result = GIT.run(["git", *args], cwd=cwd, timeout=60, env={"GIT_TERMINAL_PROMPT": "0"})
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    (source / "shop").mkdir(parents=True)
    (source / "shop" / "pricing.py").write_text(
        "def discount(total):\n    return total * 0.1\n", encoding="utf-8"
    )
    (source / "README.md").write_text("Fictitious shop\n", encoding="utf-8")
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Test User")
    git(source, "config", "user.email", "test@example.invalid")
    git(source, "config", "commit.gpgsign", "false")
    git(source, "add", "-A")
    git(source, "commit", "--no-verify", "-m", "initial")
    return source


def make_repo(**overrides) -> AuthorizedRepository:
    values = dict(
        id=1,
        application_id=1,
        name="Shop Repo",
        base_branch="main",
        test_command=["python", "-m", "pytest", "-q"],
    )
    values.update(overrides)
    return AuthorizedRepository(**values)


def snapshot(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): read(p)
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def assert_source_unchanged(source: Path, before: dict[str, str]) -> None:
    assert git(source, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(source, "status", "--porcelain") == ""
    assert git(source, "branch", "--list", "sentinel/*") == ""
    assert snapshot(source) == before


def test_clone_creates_isolated_copy_on_fix_branch(tmp_path, source_repo):
    workdir = tmp_path / "workdir"
    before = snapshot(source_repo)
    repo = make_repo(clone_url=str(source_repo))

    ws = create_isolated_copy(repo, workdir, GIT, incident_id=7)

    assert ws.branch == "sentinel/fix-7-1"
    assert ws.series == 1
    assert ws.base_branch == "main"
    assert ws.path.is_dir()
    assert workdir.resolve() in ws.path.parents
    assert ws.path != source_repo.resolve()
    assert git(ws.path, "rev-parse", "--abbrev-ref", "HEAD") == "sentinel/fix-7-1"
    assert read(ws.path / "shop" / "pricing.py") == before["shop/pricing.py"]

    # Changes in the copy never reach the source repository.
    (ws.path / "shop" / "pricing.py").write_text("changed\n", encoding="utf-8")
    git(ws.path, "add", "-A")
    git(ws.path, "commit", "--no-verify", "-m", "change in copy")
    assert_source_unchanged(source_repo, before)


def test_local_copy_initialises_git_and_skips_unsafe_files(tmp_path, source_repo):
    (source_repo / ".env").write_text("SECRET=fictitious\n", encoding="utf-8")
    (source_repo / "shop" / "__pycache__").mkdir()
    (source_repo / "shop" / "__pycache__" / "pricing.cpython-313.pyc").write_bytes(b"\x00")
    before = snapshot(source_repo)
    workdir = tmp_path / "workdir"
    repo = make_repo(local_path=str(source_repo))

    ws = create_isolated_copy(repo, workdir, GIT, incident_id=7)

    assert ws.branch == "sentinel/fix-7-1"
    assert workdir.resolve() in ws.path.parents
    assert git(ws.path, "rev-parse", "--abbrev-ref", "HEAD") == "sentinel/fix-7-1"
    assert git(ws.path, "log", "--format=%s") == "Sentinel AI: initial snapshot"
    assert git(ws.path, "status", "--porcelain") == ""
    assert read(ws.path / "shop" / "pricing.py") == before["shop/pricing.py"]
    assert read(ws.path / "README.md") == "Fictitious shop\n"
    assert not (ws.path / ".env").exists()
    assert not (ws.path / "shop" / "__pycache__").exists()
    # The copy has its own repository, not the source's history.
    assert git(ws.path, "branch", "--list", "main") != ""
    assert (ws.path / ".git").is_dir()

    # The source keeps its branch and every file, including the skipped ones.
    assert git(source_repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(source_repo, "branch", "--list", "sentinel/*") == ""
    assert snapshot(source_repo) == before


def test_second_copy_for_same_incident_uses_next_suffix(tmp_path, source_repo):
    workdir = tmp_path / "workdir"
    before = snapshot(source_repo)
    repo = make_repo(clone_url=str(source_repo))

    first = create_isolated_copy(repo, workdir, GIT, incident_id=7)
    second = create_isolated_copy(repo, workdir, GIT, incident_id=7)
    other = create_isolated_copy(repo, workdir, GIT, incident_id=8)

    assert first.branch == "sentinel/fix-7-1"
    assert second.branch == "sentinel/fix-7-2"
    assert second.series == 2
    assert other.branch == "sentinel/fix-8-1"
    assert len({first.path, second.path, other.path}) == 3
    assert git(second.path, "rev-parse", "--abbrev-ref", "HEAD") == "sentinel/fix-7-2"
    assert_source_unchanged(source_repo, before)


def test_branch_name_format():
    assert branch_name(12, 3) == "sentinel/fix-12-3"


@pytest.mark.parametrize(
    "repo",
    [
        object(),
        AuthorizedRepository(name="unsaved", test_command=["pytest"], local_path="."),
        AuthorizedRepository(id=1, application_id=1, name="empty", test_command=["pytest"]),
    ],
)
def test_refuses_unauthorized_repositories(tmp_path, repo):
    fake = FakeRunner()
    with pytest.raises(WorkspaceError):
        create_isolated_copy(repo, tmp_path / "workdir", fake, incident_id=1)
    assert fake.calls == []


def test_refuses_repository_of_another_application(tmp_path, source_repo):
    fake = FakeRunner()
    repo = make_repo(application_id=2, local_path=str(source_repo))
    with pytest.raises(WorkspaceError):
        create_isolated_copy(repo, tmp_path / "workdir", fake, incident_id=1, application_id=1)
    assert fake.calls == []


def test_refuses_workdir_inside_source_repository(tmp_path, source_repo):
    fake = FakeRunner()
    repo = make_repo(local_path=str(source_repo))
    with pytest.raises(WorkspaceError):
        create_isolated_copy(repo, source_repo / "var" / "workdir", fake, incident_id=1)
    assert fake.calls == []


def test_refuses_unsafe_base_branch(tmp_path, source_repo):
    fake = FakeRunner()
    repo = make_repo(clone_url=str(source_repo), base_branch="--upload-pack=evil")
    with pytest.raises(WorkspaceError):
        create_isolated_copy(repo, tmp_path / "workdir", fake, incident_id=1)
    assert fake.calls == []


def test_resolve_target_dir_rejects_paths_outside_workdir(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    assert resolve_target_dir(workdir, "copy-1").parent == workdir.resolve()
    for name in ("../escape", str(tmp_path / "elsewhere"), ".", ""):
        with pytest.raises(WorkspaceError):
            resolve_target_dir(workdir, name)


def test_failed_git_command_removes_partial_copy(tmp_path, source_repo):
    workdir = tmp_path / "workdir"
    fake = FakeRunner().script(["git", "init"], CommandResult(returncode=1, stderr="init failed"))
    repo = make_repo(local_path=str(source_repo))
    with pytest.raises(WorkspaceError, match="init failed"):
        create_isolated_copy(repo, workdir, fake, incident_id=3)
    assert [p.name for p in workdir.iterdir() if not p.name.startswith(".")] == []
