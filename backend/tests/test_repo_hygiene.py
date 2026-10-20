"""Repository hygiene: no secrets, .env files or CI, and the demo faults remain.

The repository is scanned on disk (never through ``.git``), skipping
directories that are ignored and never tracked (dependencies, caches, local
data and Sentinel work directories).
"""

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

IGNORED_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".venv",
        "venv",
        "dist",
        "coverage",
        ".vite",
        "var",
    }
)
TEXT_SUFFIXES = frozenset(
    {
        ".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".json", ".md", ".toml",
        ".txt", ".yml", ".yaml", ".cfg", ".ini", ".html", ".css", ".sh", ".ps1",
        ".example",
    }
)
TEXT_NAMES = frozenset({"Dockerfile", ".gitignore"})
MAX_SCAN_BYTES = 5 * 1024 * 1024

CI_PATHS = (
    ".github/workflows",
    ".gitlab-ci.yml",
    ".circleci",
    ".travis.yml",
    "azure-pipelines.yml",
    "bitbucket-pipelines.yml",
    "Jenkinsfile",
)

# Real-looking credentials. Fictitious test keys are deliberately short.
KEY_PATTERNS = {
    "anthropic key": re.compile(r"sk-ant-[A-Za-z0-9_\-]{32,}"),
    "openai key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{40,}"),
    "github token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}"),
    "github fine-grained token": re.compile(r"github_pat_[A-Za-z0-9_]{60,}"),
    "aws access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "google api key": re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    "slack token": re.compile(r"\bxox[abpors]-[A-Za-z0-9\-]{20,}"),
    "private key": re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),
    "sentinel ingest key": re.compile(r"sk_sentinel_[A-Za-z0-9_\-]{40,}"),
}


def repository_files(root: Path = ROOT) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        files.extend(Path(dirpath) / name for name in filenames)
    return files


def _is_env_file(path: Path) -> bool:
    return path.name == ".env" or path.name.startswith(".env.")


def _is_text(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_NAMES


def find_key_leaks(files: list[Path]) -> list[str]:
    leaks: list[str] = []
    for path in files:
        if not _is_text(path) or path.stat().st_size > MAX_SCAN_BYTES:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for label, pattern in KEY_PATTERNS.items():
            if pattern.search(text):
                leaks.append(f"{path.relative_to(ROOT).as_posix()}: {label}")
    return leaks


def test_scan_covers_the_repository():
    relative = {p.relative_to(ROOT).as_posix() for p in repository_files()}
    assert "README.md" in relative
    assert "backend/app/config.py" in relative
    assert "demo-app/demo_app/main.py" in relative
    assert not any(part in IGNORED_DIRS for path in relative for part in path.split("/"))


def test_no_env_files():
    env_files = [p.relative_to(ROOT).as_posix() for p in repository_files() if _is_env_file(p)]
    assert env_files == []


def test_no_ci_configuration():
    present = [path for path in CI_PATHS if (ROOT / path).exists()]
    assert present == []


def test_no_api_keys_or_private_keys():
    assert find_key_leaks(repository_files()) == []


def test_key_patterns_detect_realistic_credentials(tmp_path):
    # Built at runtime so this file never contains a credential-shaped literal.
    samples = {
        "anthropic key": "sk-" + "ant-" + "a" * 40,
        "github token": "gh" + "p_" + "B" * 36,
        "aws access key": "AK" + "IA" + "C" * 16,
        "private key": "-----BEGIN " + "RSA PRIVATE KEY-----",
        "sentinel ingest key": "sk_" + "sentinel_" + "d" * 43,
    }
    for label, sample in samples.items():
        assert KEY_PATTERNS[label].search(f"value = '{sample}'"), label
    # Fictitious short test keys are not reported.
    assert not any(p.search("sk_sentinel_fake-test-key-shop") for p in KEY_PATTERNS.values())


def test_demo_faults_are_still_present():
    main = (ROOT / "demo-app" / "demo_app" / "main.py").read_text(encoding="utf-8")
    for marker in ("# FAULT 1", "# FAULT 2", "# FAULT 3"):
        assert marker in main, marker
    assert 'user["nickname"].upper()' in main
    assert "quantity = int(raw)" in main
    assert "raise DiscountError" in main


def test_fault_reproduction_tests_remain():
    tests = (ROOT / "demo-app" / "tests" / "test_fault_reproduction.py").read_text(encoding="utf-8")
    assert "pytestmark = pytest.mark.fault_reproduction" in tests
    for name in (
        "test_profile_with_missing_nickname_returns_500",
        "test_order_with_non_numeric_quantity_returns_500",
        "test_total_with_oversized_coupon_returns_500",
    ):
        assert f"def {name}(" in tests, name
    config = (ROOT / "demo-app" / "pyproject.toml").read_text(encoding="utf-8")
    assert "fault_reproduction:" in config
