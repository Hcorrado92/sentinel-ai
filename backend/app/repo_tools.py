"""Read-only repository tools sandboxed to an authorized repository.

``RepoReader`` lets the RCA agent list, read and search files of one
repository. Every path is resolved and must stay inside the root: absolute
paths, ``..`` traversal, symlinks leaving the root and the ``.git`` directory
are rejected with ``RepoAccessError``, as are files over the size limit.
Binary files are skipped and every returned piece of content is redacted.
"""

import fnmatch
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from app.security.redaction import redact_text

DEFAULT_MAX_FILE_BYTES = 200_000
DEFAULT_MAX_FILES = 1000
_BINARY_SNIFF_BYTES = 8192

# Python frames (``File "/srv/app/x.py", line 10``) and JS-like frames
# (``at fn (/srv/app/x.js:10:5)``).
_PY_FRAME = re.compile(r'File "(?P<path>[^"]+)", line \d+')
_JS_FRAME = re.compile(r"at (?:[^\s(]+ )?\(?(?P<path>[^\s():]+(?::\\[^\s():]+)?):\d+(?::\d+)?\)?")


class RepoAccessError(Exception):
    """Raised when a path is outside the repository or otherwise not allowed."""


def _is_git_part(part: str) -> bool:
    return part.lower() == ".git"


class RepoReader:
    def __init__(
        self,
        root: str | os.PathLike[str],
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_files: int = DEFAULT_MAX_FILES,
    ) -> None:
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise RepoAccessError(f"repository root does not exist: {root}")
        self.max_file_bytes = max_file_bytes
        self.max_files = max_files

    # -- path safety -------------------------------------------------------

    def _resolve(self, path: str) -> Path:
        """Resolve a repository-relative path, enforcing the sandbox."""
        if not isinstance(path, str):
            raise RepoAccessError("path must be a string")
        if "\x00" in path:
            raise RepoAccessError("invalid path")
        normalized = path.replace("\\", "/")
        if (
            normalized.startswith("/")
            or PurePosixPath(normalized).is_absolute()
            or PureWindowsPath(path).drive
            or Path(path).is_absolute()
        ):
            raise RepoAccessError(f"absolute paths are not allowed: {path}")
        parts = [p for p in normalized.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise RepoAccessError(f"path traversal is not allowed: {path}")
        if any(_is_git_part(p) for p in parts):
            raise RepoAccessError(f"access to .git is not allowed: {path}")
        resolved = self.root.joinpath(*parts).resolve()
        rel = self._relative(resolved)
        if rel is None:
            raise RepoAccessError(f"path leaves the repository: {path}")
        if any(_is_git_part(p) for p in rel.parts):
            raise RepoAccessError(f"access to .git is not allowed: {path}")
        return resolved

    def _relative(self, resolved: Path) -> PurePosixPath | None:
        try:
            return PurePosixPath(resolved.relative_to(self.root).as_posix())
        except ValueError:
            return None

    @staticmethod
    def _is_binary(path: Path) -> bool:
        try:
            with path.open("rb") as fh:
                chunk = fh.read(_BINARY_SNIFF_BYTES)
        except OSError:
            return True
        return b"\x00" in chunk

    def _safe_file(self, rel_path: str) -> Path | None:
        """Return the resolved file for listing/searching, or None to skip it."""
        try:
            resolved = self._resolve(rel_path)
        except RepoAccessError:
            return None
        if not resolved.is_file():
            return None
        return resolved

    # -- tools ---------------------------------------------------------------

    def list_files(self, subdir: str = "", pattern: str | None = None) -> list[str]:
        """List text files (repo-relative POSIX paths) under ``subdir``."""
        base = self._resolve(subdir) if subdir else self.root
        if not base.is_dir():
            raise RepoAccessError(f"not a directory: {subdir}")
        results: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not _is_git_part(d))
            current = Path(dirpath)
            for name in sorted(filenames):
                rel = (current / name).relative_to(self.root).as_posix()
                resolved = self._safe_file(rel)
                if resolved is None or self._is_binary(resolved):
                    continue
                if pattern and not (
                    fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern)
                ):
                    continue
                results.append(rel)
                if len(results) >= self.max_files:
                    return results
        return results

    def read_file(
        self, path: str, start_line: int | None = None, end_line: int | None = None
    ) -> str:
        """Return the (redacted) content of a file, optionally a 1-based line range."""
        resolved = self._resolve(path)
        if not resolved.is_file():
            raise RepoAccessError(f"not a file: {path}")
        if resolved.stat().st_size > self.max_file_bytes:
            raise RepoAccessError(
                f"file exceeds the size limit of {self.max_file_bytes} bytes: {path}"
            )
        if self._is_binary(resolved):
            raise RepoAccessError(f"binary files cannot be read: {path}")
        text = resolved.read_bytes().decode("utf-8", errors="replace")
        if start_line is not None or end_line is not None:
            lines = text.splitlines(keepends=True)
            start = max((start_line or 1) - 1, 0)
            end = end_line if end_line is not None else len(lines)
            text = "".join(lines[start:end])
        return redact_text(text)

    def search(self, text: str, max_results: int = 50) -> list[dict[str, object]]:
        """Find lines containing ``text``; returns ``{path, line, text}`` dicts."""
        if not text:
            return []
        matches: list[dict[str, object]] = []
        for rel in self.list_files():
            try:
                content = self.read_file(rel)
            except RepoAccessError:
                continue
            for number, line in enumerate(content.splitlines(), start=1):
                if text in line:
                    matches.append({"path": rel, "line": number, "text": line})
                    if len(matches) >= max_results:
                        return matches
        return matches

    def find_relevant_files(self, stack_trace: str) -> list[str]:
        """Map stack frames to repository files by matching path suffixes."""
        found: list[str] = []
        for frame_path in _frame_paths(stack_trace or ""):
            parts = [p for p in frame_path.replace("\\", "/").split("/") if p]
            parts = [p for p in parts if not PureWindowsPath(p + "/").drive]
            for i in range(len(parts)):
                candidate = "/".join(parts[i:])
                resolved = self._safe_file(candidate)
                if resolved is not None:
                    rel = self._relative(resolved)
                    if rel is not None and str(rel) not in found:
                        found.append(str(rel))
                    break
        return found


def _frame_paths(stack_trace: str) -> list[str]:
    paths: list[str] = []
    for regex in (_PY_FRAME, _JS_FRAME):
        for match in regex.finditer(stack_trace):
            path = match.group("path")
            if path.startswith("<") or "site-packages" in path:
                continue
            paths.append(path)
    return paths


def find_relevant_files(reader: RepoReader, stack_trace: str) -> list[str]:
    return reader.find_relevant_files(stack_trace)
