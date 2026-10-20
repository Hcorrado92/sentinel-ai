import os

import pytest

from app.repo_tools import RepoAccessError, RepoReader, find_relevant_files


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "demo_app").mkdir(parents=True)
    (root / "demo_app" / "__init__.py").write_text("", encoding="utf-8")
    # write_bytes keeps "\n" line endings on every OS (write_text emits "\r\n" on Windows).
    (root / "demo_app" / "main.py").write_bytes(
        b"def handler(item):\n    return item['price'] * 2\n"
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_main.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside secret", encoding="utf-8")
    return root


def test_rejects_parent_traversal(repo):
    reader = RepoReader(repo)
    with pytest.raises(RepoAccessError):
        reader.read_file("../outside.txt")
    with pytest.raises(RepoAccessError):
        reader.read_file("demo_app/../../outside.txt")


def test_rejects_absolute_paths(repo, tmp_path):
    reader = RepoReader(repo)
    with pytest.raises(RepoAccessError):
        reader.read_file(str(tmp_path / "outside.txt"))
    with pytest.raises(RepoAccessError):
        reader.read_file(str(repo / "demo_app" / "main.py"))
    with pytest.raises(RepoAccessError):
        reader.read_file("/etc/passwd")


def test_rejects_symlink_leaving_repo(repo, tmp_path):
    link = repo / "evil.txt"
    try:
        os.symlink(tmp_path / "outside.txt", link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not allowed on this OS")
    reader = RepoReader(repo)
    with pytest.raises(RepoAccessError):
        reader.read_file("evil.txt")
    assert "evil.txt" not in reader.list_files()


def test_rejects_git_directory(repo):
    reader = RepoReader(repo)
    with pytest.raises(RepoAccessError):
        reader.read_file(".git/config")
    with pytest.raises(RepoAccessError):
        reader.list_files(".git")
    assert not any(p.startswith(".git") for p in reader.list_files())


def test_rejects_files_over_size_limit(repo):
    (repo / "big.txt").write_text("x" * 200, encoding="utf-8")
    reader = RepoReader(repo, max_file_bytes=100)
    with pytest.raises(RepoAccessError):
        reader.read_file("big.txt")


def test_read_file_redacts_secrets(repo):
    (repo / "settings.py").write_text("api_key=XYZ\nname = 'demo'\n", encoding="utf-8")
    content = RepoReader(repo).read_file("settings.py")
    assert "XYZ" not in content
    assert "[REDACTED]" in content
    assert "name = 'demo'" in content


def test_read_file_line_range(repo):
    content = RepoReader(repo).read_file("demo_app/main.py", start_line=2, end_line=2)
    assert content == "    return item['price'] * 2\n"


def test_list_files_skips_binary_and_honours_pattern_and_limit(repo):
    (repo / "image.bin").write_bytes(b"\x89PNG\x00\x01\x02")
    reader = RepoReader(repo)
    files = reader.list_files()
    assert "demo_app/main.py" in files
    assert "image.bin" not in files
    assert reader.list_files(pattern="*.py") == [
        "demo_app/__init__.py",
        "demo_app/main.py",
        "tests/test_main.py",
    ]
    assert reader.list_files("tests") == ["tests/test_main.py"]
    assert len(RepoReader(repo, max_files=1).list_files()) == 1


def test_search_finds_lines(repo):
    results = RepoReader(repo).search("price", max_results=10)
    assert results == [
        {"path": "demo_app/main.py", "line": 2, "text": "    return item['price'] * 2"}
    ]


def test_find_relevant_files_maps_frames(repo):
    trace = (
        "Traceback (most recent call last):\n"
        '  File "/usr/lib/python3.12/site-packages/fastapi/routing.py", line 99, in run\n'
        '  File "/srv/app/demo_app/main.py", line 10, in handler\n'
        '  File "C:\\deploy\\app\\demo_app\\main.py", line 12, in handler\n'
        '  File "/srv/app/demo_app/missing.py", line 3, in nope\n'
        "TypeError: 'NoneType' object is not subscriptable\n"
    )
    reader = RepoReader(repo)
    assert reader.find_relevant_files(trace) == ["demo_app/main.py"]
    assert find_relevant_files(reader, trace) == ["demo_app/main.py"]
    assert reader.find_relevant_files("") == []
