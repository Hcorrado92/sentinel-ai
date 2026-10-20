import logging
import string

import pytest

from app.security.redaction import (
    REDACTED,
    RedactingFilter,
    is_sensitive_key,
    redact_obj,
    redact_text,
)


def synthetic_credential(prefix: str, length: int, alphabet: str = string.ascii_letters + string.digits) -> str:
    """Fake credential in a real key format, built at runtime.

    No key-like literal is stored in the repository, so secret scanners never flag
    the test sources, while the redaction patterns still see realistic values.
    """
    return prefix + (alphabet * (length // len(alphabet) + 1))[:length]


FAKE_ANTHROPIC_KEY = synthetic_credential("sk-ant-", 24)
FAKE_GITHUB_TOKEN = synthetic_credential("ghp_", 36)
FAKE_AWS_KEY_ID = synthetic_credential("AKIA", 16, string.ascii_uppercase + string.digits)
FAKE_SLACK_TOKEN = synthetic_credential("xoxb-", 24)


def test_authorization_bearer_header_is_redacted():
    result = redact_text("Authorization: Bearer abc.def.ghi")
    assert REDACTED in result
    assert "abc.def.ghi" not in result
    assert result.startswith("Authorization:")


def test_bare_bearer_token_is_redacted():
    result = redact_text("calling upstream with bearer eyJhbGciOi.payload.sig failed")
    assert "eyJhbGciOi.payload.sig" not in result
    assert result.startswith("calling upstream with bearer [REDACTED]")
    assert result.endswith(" failed")


def test_json_authorization_header_is_redacted():
    result = redact_text('{"Authorization": "Basic dXNlcjpwYXNz", "Accept": "json"}')
    assert "dXNlcjpwYXNz" not in result
    assert '"Accept": "json"' in result


def test_url_credentials_are_redacted_and_host_kept():
    result = redact_text("postgres://admin:hunter2@db:5432/x")
    assert "hunter2" not in result
    assert "db:5432" in result
    assert result == "postgres://admin:[REDACTED]@db:5432/x"


def test_url_without_credentials_is_unchanged():
    text = "GET http://localhost:8080/api/orders?page=2"
    assert redact_text(text) == text


@pytest.mark.parametrize("key", ["password", "secret", "token", "api_key"])
@pytest.mark.parametrize("case", [str.lower, str.upper])
def test_key_value_pairs_are_redacted(key, case):
    name = case(key)
    result = redact_text(f"{name}=foo&user=bob")
    assert "foo" not in result
    assert "user=bob" in result
    assert result == f"{name}=[REDACTED]&user=bob"


@pytest.mark.parametrize(
    "text, leaked",
    [
        ("passwd: s3cr3t", "s3cr3t"),
        ("client_secret = 'abcd1234'", "abcd1234"),
        ('{"apikey": "zz-99"}', "zz-99"),
        ("ACCESS_KEY=AK123 other=1", "AK123"),
        ("private_key: \"-----KEY-----\"", "-----KEY-----"),
        ("db_password=letmein", "letmein"),
    ],
)
def test_other_sensitive_pairs_are_redacted(text, leaked):
    result = redact_text(text)
    assert leaked not in result
    assert REDACTED in result


@pytest.mark.parametrize(
    "secret",
    [
        FAKE_ANTHROPIC_KEY,
        FAKE_GITHUB_TOKEN,
        FAKE_AWS_KEY_ID,
        FAKE_SLACK_TOKEN,
    ],
)
def test_common_api_key_formats_are_redacted(secret):
    result = redact_text(f"key used was {secret} in request")
    assert secret not in result
    assert result == f"key used was {REDACTED} in request"


def test_redact_obj_walks_nested_structures():
    result = redact_obj({"api_key": "x1", "nested": [{"msg": "token=abc"}], "count": 3})
    assert result["api_key"] == REDACTED
    assert result["nested"][0]["msg"] == "token=[REDACTED]"
    assert result["count"] == 3
    assert "x1" not in repr(result)
    assert "abc" not in repr(result)


def test_redact_obj_handles_tuples_and_none():
    result = redact_obj({"password": None, "items": ("secret=s1", 2, None)})
    assert result == {"password": None, "items": ("secret=[REDACTED]", 2, None)}


def test_sensitive_key_detection_ignores_token_counters():
    assert is_sensitive_key("accessToken")
    assert is_sensitive_key("X-Api-Key")
    assert is_sensitive_key("Authorization")
    assert not is_sensitive_key("input_tokens")
    assert not is_sensitive_key("token_count")
    assert not is_sensitive_key("count")
    assert redact_obj({"input_tokens": 120})["input_tokens"] == 120


SAMPLES = [
    "Authorization: Bearer abc.def.ghi",
    "postgres://admin:hunter2@db:5432/x",
    "password=foo&user=bob",
    'SECRET="a \\"quoted\\" value" TOKEN=x API_KEY: y',
    "token=Bearer abc123",
    f"{FAKE_ANTHROPIC_KEY} and {FAKE_GITHUB_TOKEN}",
    "redis://:pw@cache:6379/0?api_key=k",
]


@pytest.mark.parametrize("text", SAMPLES)
def test_redaction_is_idempotent(text):
    once = redact_text(text)
    assert redact_text(once) == once


def test_redact_obj_is_idempotent():
    data = {"api_key": "x1", "nested": [{"msg": "token=abc"}], "count": 3, "url": SAMPLES[1]}
    once = redact_obj(data)
    assert redact_obj(once) == once


@pytest.mark.parametrize(
    "text",
    [
        "",
        "ValueError: invalid literal for int() with base 10: 'abc'",
        "GET /api/orders?user=bob&page=2 took 12ms",
        'File "app/orders.py", line 42, in compute_discount',
        "used input_tokens: 120, output_tokens: 30",
        "the secret ingredient is love",
    ],
)
def test_text_without_secrets_is_unchanged(text):
    assert redact_text(text) == text


def test_obj_without_secrets_is_unchanged():
    data = {"user": "bob", "items": [1, 2, {"sku": "A-1"}], "ok": True}
    assert redact_obj(data) == data


def test_logging_filter_redacts_messages():
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "connect %s", ("password=foo",), None)
    assert RedactingFilter().filter(record)
    assert record.getMessage() == "connect password=[REDACTED]"
