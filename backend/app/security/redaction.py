"""Centralized secret redaction.

``redact_text`` scrubs secrets out of free text and ``redact_obj`` walks
dicts/lists/tuples recursively, redacting strings and the values of keys that
look sensitive. Both are idempotent (redacting twice equals redacting once)
and leave text without secrets untouched. They are reused by ingestion,
prompts, reports and logs; ``RedactingFilter`` plugs them into ``logging``.
"""

import logging
import re
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_WORDS = (
    r"password|passwd|secret|token|api[_\-]?key|apikey|access[_\-]?key"
    r"|private[_\-]?key|client[_\-]?secret"
)

# Quoted value (handles backslash escapes) or a bare value up to a delimiter.
_VALUE = (
    r"(?:(?P<q>[\"'])(?P<qval>(?:\\.|[^\\])*?)(?P=q)"
    r"|(?P<val>[^\s&;,\"'<>(){}]+))"
)

# Authorization / Proxy-Authorization headers, e.g. ``Authorization: Basic xyz``.
_AUTH_HEADER = re.compile(
    r"(?<![A-Za-z0-9_\-])(?P<key>(?:proxy-)?authorization)(?P<kq>[\"']?)"
    r"(?P<sep>[ \t]*[:=][ \t]*)"
    r"(?:(?P<q>[\"'])(?P<qval>(?:\\.|[^\\])*?)(?P=q)"
    r"|(?P<val>(?:(?:bearer|basic|token|digest|negotiate|apikey)[ \t]+)?[^\s\"',;]+))",
    re.IGNORECASE,
)

_BEARER = re.compile(r"\b(?P<scheme>bearer)\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)

_API_KEY_FORMATS = re.compile(
    r"\bsk[-_][A-Za-z0-9_\-]{16,}"  # OpenAI / Anthropic / Stripe style
    r"|\bgh[pousr]_[A-Za-z0-9]{20,}"  # GitHub tokens
    r"|\bgithub_pat_[A-Za-z0-9_]{20,}"
    r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"  # AWS access key ids
    r"|\bxox[abposr]-[A-Za-z0-9\-]{10,}"  # Slack tokens
)

# scheme://user:pass@host -> keeps scheme, user and host.
_URL_CREDENTIALS = re.compile(
    r"(?P<prefix>\b[A-Za-z][A-Za-z0-9+.\-]*://[^\s/:@]*:)(?P<pw>[^\s/?#]+)@"
)

_KEY_VALUE = re.compile(
    r"(?<![A-Za-z0-9_.\-])(?P<key>[A-Za-z0-9_.\-]*?(?:" + _SENSITIVE_WORDS + r"))"
    r"(?![A-Za-z0-9_\-])(?P<kq>[\"']?)(?P<sep>[ \t]*[:=][ \t]*)" + _VALUE,
    re.IGNORECASE,
)

# Dict keys whose values are redacted wholesale. Matched against the normalized
# key's last part (so ``input_tokens`` or ``token_count`` are not secrets),
# except for words that are sensitive anywhere in the key.
_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:pwd|token|api_?key|access_?key|private_?key|client_?secret|auth"
    r"|authorization|cookie|session_?id)$"
    r"|(?:^|_)(?:password|passwd|secret|credentials?)(?:_|$)"
)


def _replace_value(match: re.Match[str]) -> str:
    if match.group("q") is not None:
        if match.group("qval") == "":
            return match.group(0)
        quote = match.group("q")
        value = quote + REDACTED + quote
    else:
        value = REDACTED
    return match.group("key") + match.group("kq") + match.group("sep") + value


def redact_text(text: str) -> str:
    """Return ``text`` with secrets replaced by ``[REDACTED]``."""
    if not text:
        return text
    text = _AUTH_HEADER.sub(_replace_value, text)
    text = _BEARER.sub(lambda m: m.group("scheme") + " " + REDACTED, text)
    text = _API_KEY_FORMATS.sub(REDACTED, text)
    text = _URL_CREDENTIALS.sub(lambda m: m.group("prefix") + REDACTED + "@", text)
    text = _KEY_VALUE.sub(_replace_value, text)
    return text


def is_sensitive_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized.lower()).strip("_")
    return bool(_SENSITIVE_KEY.search(normalized))


def redact_obj(obj: Any) -> Any:
    """Recursively redact strings in dicts, lists, tuples and sets.

    Values of sensitive-looking dict keys are replaced entirely (``None`` is
    kept). Non-string scalars are returned unchanged.
    """
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {
            key: (REDACTED if value is not None else None)
            if is_sensitive_key(key)
            else redact_obj(value)
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [redact_obj(item) for item in obj]
    if isinstance(obj, tuple):
        return tuple(redact_obj(item) for item in obj)
    if isinstance(obj, (set, frozenset)):
        return type(obj)(redact_obj(item) for item in obj)
    return obj


class RedactingFilter(logging.Filter):
    """Logging filter that redacts the formatted message of every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact_text(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True
