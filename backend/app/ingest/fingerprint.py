"""Stable fingerprints for error events.

The fingerprint is the SHA-256 of the exception type plus the normalized
frames of the stack trace. Messages are never part of it, so variable values
in messages do not split incidents.

Parsing:

- Python tracebacks: ``File "path", line N, in func`` lines become
  ``path:func`` frames.
- Generic fallback (other languages, free-form traces): lines starting with
  ``at `` (Java/JS/.NET style) are used as frames; when there are none, every
  non-empty line except the traceback header and the exception/message line.

Normalization drops line and column numbers, memory addresses, UUIDs and
other numbers; site-packages / stdlib paths are reduced to the path inside
the package directory and absolute paths to their last two components, so the
same code deployed at different locations yields the same fingerprint.
"""

import hashlib
import re

MAX_FRAMES = 100

_PY_FRAME = re.compile(r'^\s*File "(?P<path>[^"]+)", line \d+(?:, in (?P<func>.+?))?\s*$')
_GENERIC_FRAME = re.compile(r"^\s*at\s+(?P<frame>.+?)\s*$")

_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_ADDRESS = re.compile(r"\b0x[0-9a-fA-F]+\b")
_LINE_REF = re.compile(r"\bline \d+\b")
_POSITION = re.compile(r":\d+(?::\d+)?\b")
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
_PATH_TOKEN = re.compile(r"(?:[A-Za-z]:)?[\w.\-<>@~]*(?:[\\/][\w.\-<>@~]+)+")
_PACKAGE_DIR = re.compile(r"(?:^|(?<=/))(?:site-packages|dist-packages|lib/python\d+(?:\.\d+)?)/")
_ABSOLUTE = re.compile(r"^(?:/|[A-Za-z]:/|~/)")


def normalize_path(path: str) -> str:
    path = path.strip().replace("\\", "/")
    matches = list(_PACKAGE_DIR.finditer(path))
    if matches:
        return path[matches[-1].end() :]
    if _ABSOLUTE.match(path):
        parts = [part for part in path.split("/") if part and not part.endswith(":")]
        return "/".join(parts[-2:])
    return path.removeprefix("./")


def normalize_text(text: str) -> str:
    """Remove variable values (UUIDs, addresses, line numbers, numbers)."""
    text = _UUID.sub("<uuid>", text)
    text = _ADDRESS.sub("<addr>", text)
    text = _LINE_REF.sub("line", text)
    text = _POSITION.sub("", text)
    text = _NUMBER.sub("<n>", text)
    return " ".join(text.split())


def _normalize_generic(line: str) -> str:
    line = _UUID.sub("<uuid>", line)
    line = _PATH_TOKEN.sub(lambda match: normalize_path(match.group(0)), line)
    return normalize_text(line)


def _python_frames(stack_trace: str) -> list[str]:
    frames = []
    for line in stack_trace.splitlines():
        match = _PY_FRAME.match(line)
        if match:
            path = normalize_text(normalize_path(match.group("path")))
            func = normalize_text(match.group("func") or "")
            frames.append(f"{path}:{func}")
    return frames


def _generic_frames(stack_trace: str, exception_type: str) -> list[str]:
    lines = [line for line in stack_trace.splitlines() if line.strip()]
    at_frames = [m.group("frame") for m in map(_GENERIC_FRAME.match, lines) if m]
    if at_frames:
        return [_normalize_generic(frame) for frame in at_frames]

    short_type = exception_type.rsplit(".", 1)[-1]
    frames = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Traceback (most recent call last)"):
            continue
        head = stripped.split(":", 1)[0].strip()
        if head in (exception_type, short_type):
            continue
        frames.append(_normalize_generic(stripped))
    return frames


def parse_frames(stack_trace: str, exception_type: str = "") -> list[str]:
    """Return the normalized frames of ``stack_trace`` (outermost first)."""
    frames = _python_frames(stack_trace) or _generic_frames(stack_trace, exception_type)
    return [frame for frame in frames if frame][:MAX_FRAMES]


def compute_fingerprint(exception_type: str, stack_trace: str) -> str:
    exception_type = exception_type.strip()
    frames = parse_frames(stack_trace, exception_type)
    material = "\n".join([exception_type, *frames])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
