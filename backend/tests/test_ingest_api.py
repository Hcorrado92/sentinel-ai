import json

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.models import Application, AuditLog, Event, Incident
from app.security.auth import hash_ingest_key
from app.security.redaction import REDACTED

URL = "/api/v1/ingest/events"
KEY_A = "sk_sentinel_fake-test-key-shop"
KEY_B = "sk_sentinel_fake-test-key-blog"

MAX_BODY = 16 * 1024


@pytest.fixture
def settings(tmp_path, database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        workdir=str(tmp_path / "workdir"),
        max_body_bytes=MAX_BODY,
        max_message_length=200,
        max_stack_trace_length=2000,
        max_metadata_items=5,
        max_metadata_bytes=500,
    )


@pytest.fixture
def apps(app) -> dict[str, int]:
    with app.state.session_factory() as session:
        shop = Application(name="Demo Shop", slug="demo-shop", ingest_key_hash=hash_ingest_key(KEY_A))
        blog = Application(name="Demo Blog", slug="demo-blog", ingest_key_hash=hash_ingest_key(KEY_B))
        session.add_all([shop, blog])
        session.commit()
        return {"demo-shop": shop.id, "demo-blog": blog.id}


def _event(**overrides) -> dict:
    event = {
        "application": "demo-shop",
        "event_id": "evt-001",
        "timestamp": "2024-01-01T10:00:00Z",
        "exception_type": "ZeroDivisionError",
        "message": "division by zero",
        "stack_trace": 'Traceback (most recent call last):\n  File "shop/cart.py", line 10, in total\n',
        "environment": "test",
        "correlation_id": "req-123",
        "metadata": {"path": "/cart", "status": 500, "retry": False},
    }
    event.update(overrides)
    return event


def _post(client, event, key=KEY_A, **kwargs):
    headers = {"X-Sentinel-Key": key} if key is not None else {}
    return client.post(URL, json=event, headers=headers, **kwargs)


def _event_count(app) -> int:
    with app.state.session_factory() as session:
        return session.scalar(select(func.count()).select_from(Event))


def test_valid_event_is_accepted_and_stored_redacted(client, app, apps):
    event = _event(
        message="login failed password=hunter2 with Authorization: Bearer abc.def.ghi",
        stack_trace='File "db.py", line 3, in connect\n  postgres://shop:s3cr3tpw@db.local/shop\n',
        metadata={
            "api_key": "fake-value-123",
            "url": "https://example.test/cb?token=abc123",
            "count": 3,
            "ratio": 0.5,
            "flag": True,
        },
    )
    response = _post(client, event)
    assert response.status_code == 202
    body = response.json()
    assert body["event_id"] == "evt-001"
    assert body["deduplicated"] is False
    assert isinstance(body["incident_id"], int)

    with app.state.session_factory() as session:
        stored = session.scalars(select(Event)).one()
        assert stored.application_id == apps["demo-shop"]
        assert stored.incident_id == body["incident_id"]
        assert stored.exception_type == "ZeroDivisionError"
        assert stored.environment == "test"
        assert stored.correlation_id == "req-123"

        assert "hunter2" not in stored.message
        assert "abc.def.ghi" not in stored.message
        assert REDACTED in stored.message
        assert "s3cr3tpw" not in stored.stack_trace
        assert REDACTED in stored.stack_trace
        assert stored.metadata_json["api_key"] == REDACTED
        assert "abc123" not in stored.metadata_json["url"]
        assert REDACTED in stored.metadata_json["url"]
        assert stored.metadata_json["count"] == 3
        assert stored.metadata_json["ratio"] == 0.5
        assert stored.metadata_json["flag"] is True

        incident = session.get(Incident, body["incident_id"])
        assert incident.application_id == apps["demo-shop"]
        assert incident.exception_type == "ZeroDivisionError"
        assert incident.event_count == 1
        assert incident.status == "open"
        assert "hunter2" not in incident.title

        audit = session.scalars(select(AuditLog).where(AuditLog.action == "event_received")).one()
        assert audit.application_id == apps["demo-shop"]
        assert audit.incident_id == body["incident_id"]
        assert audit.details["event_id"] == "evt-001"


def test_events_with_same_exception_type_share_provisional_incident(client, app, apps):
    first = _post(client, _event(event_id="evt-1", timestamp="2024-01-01T10:00:00Z")).json()
    second = _post(client, _event(event_id="evt-2", timestamp="2024-01-01T11:00:00Z")).json()
    other = _post(client, _event(event_id="evt-3", exception_type="KeyError")).json()

    assert first["incident_id"] == second["incident_id"]
    assert other["incident_id"] != first["incident_id"]
    with app.state.session_factory() as session:
        incident = session.get(Incident, first["incident_id"])
        assert incident.event_count == 2
        assert incident.last_seen.replace(tzinfo=None).hour == 11
        assert incident.first_seen.replace(tzinfo=None).hour == 10


def test_repeated_event_id_is_deduplicated(client, app, apps):
    first = _post(client, _event())
    again = _post(client, _event())
    assert first.status_code == 202
    assert again.status_code == 202
    assert again.json() == {**first.json(), "deduplicated": True}
    assert _event_count(app) == 1
    with app.state.session_factory() as session:
        incident = session.get(Incident, first.json()["incident_id"])
        assert incident.event_count == 1


def test_event_without_event_id_gets_one(client, app, apps):
    event = _event()
    del event["event_id"]
    response = _post(client, event)
    assert response.status_code == 202
    assert response.json()["event_id"]


def test_missing_key_returns_401(client, app, apps):
    assert _post(client, _event(), key=None).status_code == 401
    assert _event_count(app) == 0


def test_unknown_key_returns_401(client, app, apps):
    assert _post(client, _event(), key="sk_sentinel_not-a-real-key").status_code == 401
    assert _event_count(app) == 0


def test_key_of_other_application_returns_403_and_stores_nothing(client, app, apps):
    response = _post(client, _event(application="demo-blog"), key=KEY_A)
    assert response.status_code == 403
    assert _event_count(app) == 0
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Incident)) == 0


def test_body_over_limit_returns_413(client, app, apps):
    response = _post(client, _event(metadata={"blob": "x" * (MAX_BODY + 10)}))
    assert response.status_code == 413
    assert _event_count(app) == 0


def test_streamed_body_over_limit_without_content_length_returns_413(client, app, apps):
    payload = json.dumps(_event(message="y" * (MAX_BODY + 10))).encode("utf-8")

    def chunks():
        for start in range(0, len(payload), 1024):
            yield payload[start : start + 1024]

    response = client.post(
        URL,
        content=chunks(),
        headers={"X-Sentinel-Key": KEY_A, "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert _event_count(app) == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"message": "m" * 201},
        {"stack_trace": "s" * 2001},
        {"metadata": {f"k{i}": i for i in range(6)}},
        {"metadata": {"long": "v" * 600}},
        {"metadata": {"nested": {"inner": 1}}},
        {"metadata": {"items": [1, 2]}},
    ],
    ids=["message", "stack_trace", "metadata_keys", "metadata_value_size", "nested_dict", "list"],
)
def test_limits_and_shape_violations_return_422(client, app, apps, overrides):
    response = _post(client, _event(**overrides))
    assert response.status_code == 422
    assert _event_count(app) == 0


@pytest.mark.parametrize("field", ["exception_type", "timestamp"])
def test_missing_required_fields_return_422(client, app, apps, field):
    event = _event()
    del event[field]
    response = _post(client, event)
    assert response.status_code == 422
    assert any(field in error["loc"] for error in response.json()["detail"])
    assert _event_count(app) == 0


def test_invalid_json_returns_422(client, app, apps):
    response = client.post(
        URL,
        content=b"{not json",
        headers={"X-Sentinel-Key": KEY_A, "Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_validation_errors_do_not_echo_secrets(client, app, apps):
    response = _post(client, _event(message="password=hunter2 " + "m" * 300))
    assert response.status_code == 422
    assert "hunter2" not in response.text
