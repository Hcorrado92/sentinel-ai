from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.models import Application, AuditLog, Event, Incident
from app.security.auth import hash_ingest_key

URL = "/api/v1/ingest/events"
KEY_SHOP = "sk_sentinel_fake-grouping-key-shop"
KEY_BLOG = "sk_sentinel_fake-grouping-key-blog"
KEYS = {"demo-shop": KEY_SHOP, "demo-blog": KEY_BLOG}

TRACE = (
    "Traceback (most recent call last):\n"
    '  File "/srv/app/shop/orders.py", line {line}, in create_order\n'
    '  File "/srv/app/shop/pricing.py", line {inner}, in apply_discount\n'
    "TypeError: unsupported operand type(s) at 0x{addr}\n"
)


@pytest.fixture
def apps(app) -> dict[str, int]:
    with app.state.session_factory() as session:
        shop = Application(
            name="Demo Shop", slug="demo-shop", ingest_key_hash=hash_ingest_key(KEY_SHOP)
        )
        blog = Application(
            name="Demo Blog", slug="demo-blog", ingest_key_hash=hash_ingest_key(KEY_BLOG)
        )
        session.add_all([shop, blog])
        session.commit()
        return {"demo-shop": shop.id, "demo-blog": blog.id}


def _event(n: int, **overrides) -> dict:
    event = {
        "application": "demo-shop",
        "event_id": f"evt-{n}",
        "timestamp": "2024-03-01T10:00:00Z",
        "exception_type": "TypeError",
        "message": f"bad total for order {n}",
        "stack_trace": TRACE.format(line=10 + n, inner=40 + n, addr=f"{n:08x}"),
        "environment": "development",
    }
    event.update(overrides)
    return event


def _post(client, event):
    response = client.post(URL, json=event, headers={"X-Sentinel-Key": KEYS[event["application"]]})
    assert response.status_code == 202, response.text
    return response.json()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=None)


def test_three_events_with_same_fingerprint_create_one_incident(client, app, apps):
    results = [
        _post(client, _event(1, timestamp="2024-03-01T11:00:00Z")),
        _post(client, _event(2, timestamp="2024-03-01T09:00:00Z")),
        _post(client, _event(3, timestamp="2024-03-01T10:30:00Z")),
    ]
    assert len({r["incident_id"] for r in results}) == 1
    assert all(r["deduplicated"] is False for r in results)

    with app.state.session_factory() as session:
        incidents = session.scalars(select(Incident)).all()
        assert len(incidents) == 1
        incident = incidents[0]
        assert incident.event_count == 3
        assert _utc(incident.first_seen) == datetime(2024, 3, 1, 9, 0)
        assert _utc(incident.last_seen) == datetime(2024, 3, 1, 11, 0)
        assert session.scalar(select(func.count()).select_from(Event)) == 3


def test_different_function_creates_separate_incident(client, app, apps):
    first = _post(client, _event(1))
    other_trace = TRACE.format(line=1, inner=2, addr="ff").replace(
        "apply_discount", "apply_coupon"
    )
    second = _post(client, _event(2, stack_trace=other_trace))
    assert first["incident_id"] != second["incident_id"]


def test_same_fingerprint_in_two_applications_creates_two_incidents(client, app, apps):
    shop = _post(client, _event(1))
    blog = _post(client, _event(1, application="demo-blog"))
    assert shop["deduplicated"] is False
    assert blog["deduplicated"] is False
    assert shop["incident_id"] != blog["incident_id"]

    with app.state.session_factory() as session:
        incidents = session.scalars(select(Incident)).all()
        assert len(incidents) == 2
        assert {i.application_id for i in incidents} == set(apps.values())
        assert len({i.fingerprint for i in incidents}) == 1
        assert all(i.event_count == 1 for i in incidents)


def test_reposted_event_id_is_deduplicated_and_count_unchanged(client, app, apps):
    first = _post(client, _event(1))
    _post(client, _event(2))
    again = _post(client, _event(1, timestamp="2024-03-02T10:00:00Z"))

    assert again["deduplicated"] is True
    assert again["incident_id"] == first["incident_id"]
    assert again["event_id"] == "evt-1"
    with app.state.session_factory() as session:
        incident = session.get(Incident, first["incident_id"])
        assert incident.event_count == 2
        assert _utc(incident.last_seen) == datetime(2024, 3, 1, 10, 0)
        assert session.scalar(select(func.count()).select_from(Event)) == 2


def test_severity_reflects_environment_and_frequency(client, app, apps):
    dev = _post(client, _event(1))
    with app.state.session_factory() as session:
        assert session.get(Incident, dev["incident_id"]).severity == "high"

    for n in range(2, 11):
        _post(client, _event(n, exception_type="ValueError", stack_trace="ValueError: x"))
    with app.state.session_factory() as session:
        incident = session.scalars(
            select(Incident).where(Incident.exception_type == "ValueError")
        ).one()
        assert incident.event_count == 9
        assert incident.severity == "medium"

    _post(client, _event(11, exception_type="ValueError", stack_trace="ValueError: x"))
    with app.state.session_factory() as session:
        incident = session.scalars(
            select(Incident).where(Incident.exception_type == "ValueError")
        ).one()
        assert incident.event_count == 10
        assert incident.severity == "high"

    prod = _post(client, _event(1, application="demo-blog", environment="production"))
    with app.state.session_factory() as session:
        assert session.get(Incident, prod["incident_id"]).severity == "critical"


def test_resolved_incident_is_reopened_as_regressed(client, app, apps):
    first = _post(client, _event(1))
    with app.state.session_factory() as session:
        incident = session.get(Incident, first["incident_id"])
        incident.status = "resolved"
        incident.resolved_at = datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc)
        session.commit()

    again = _post(client, _event(2, timestamp="2024-03-02T08:00:00Z"))
    assert again["incident_id"] == first["incident_id"]

    with app.state.session_factory() as session:
        incident = session.get(Incident, first["incident_id"])
        assert incident.status == "regressed"
        assert incident.event_count == 2
        assert incident.resolved_at is None
        audits = session.scalars(
            select(AuditLog).where(AuditLog.action == "incident_regressed")
        ).all()
        assert len(audits) == 1
        assert audits[0].incident_id == incident.id
        assert audits[0].application_id == apps["demo-shop"]
        assert audits[0].details["event_id"] == "evt-2"


def test_open_incident_receiving_events_is_not_marked_regressed(client, app, apps):
    first = _post(client, _event(1))
    _post(client, _event(2))
    with app.state.session_factory() as session:
        assert session.get(Incident, first["incident_id"]).status == "open"
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "incident_regressed")
            )
            == 0
        )
