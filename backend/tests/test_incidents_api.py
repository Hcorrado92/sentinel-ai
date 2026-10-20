from datetime import datetime, timedelta, timezone

import pytest

from app.clock import FakeClock
from app.config import Settings
from app.main import create_app
from app.models import Application, Incident
from app.security.auth import hash_ingest_key
from app.security.redaction import REDACTED

ADMIN_TOKEN = "fake-admin-token-for-tests"
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
INGEST_URL = "/api/v1/ingest/events"
KEY_SHOP = "sk_sentinel_fake-dashboard-key-shop"
KEY_BLOG = "sk_sentinel_fake-dashboard-key-blog"
KEYS = {"demo-shop": KEY_SHOP, "demo-blog": KEY_BLOG}

TRACE = (
    "Traceback (most recent call last):\n"
    '  File "/srv/app/shop/{module}.py", line 12, in {function}\n'
    "{exc}: boom\n"
)


@pytest.fixture
def settings(tmp_path, database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        workdir=str(tmp_path / "workdir"),
        admin_token=ADMIN_TOKEN,
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc))


@pytest.fixture
def app(settings: Settings, clock: FakeClock):
    application = create_app(settings, clock=clock)
    yield application
    application.state.engine.dispose()


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


def _post(client, *, n: int, application="demo-shop", function="create_order",
          exc="TypeError", timestamp="2024-03-01T10:00:00Z", environment="development",
          message=None, metadata=None):
    event = {
        "application": application,
        "event_id": f"evt-{application}-{n}",
        "timestamp": timestamp,
        "exception_type": exc,
        "message": message or f"failure {n}",
        "stack_trace": TRACE.format(module="orders", function=function, exc=exc),
        "environment": environment,
    }
    if metadata is not None:
        event["metadata"] = metadata
    response = client.post(INGEST_URL, json=event, headers={"X-Sentinel-Key": KEYS[application]})
    assert response.status_code == 202, response.text
    return response.json()["incident_id"]


@pytest.fixture
def three_incidents(client, apps) -> dict[str, int]:
    old = _post(client, n=1, function="old_fn", exc="ValueError", timestamp="2024-03-01T08:00:00Z")
    new = _post(client, n=2, function="new_fn", exc="TypeError", timestamp="2024-03-01T11:00:00Z")
    mid = _post(
        client, n=3, application="demo-blog", function="mid_fn", exc="KeyError",
        timestamp="2024-03-01T09:30:00Z", environment="production",
    )
    return {"old": old, "new": new, "mid": mid}


def test_incidents_require_admin_token(client, three_incidents):
    assert client.get("/api/v1/incidents").status_code == 401
    bad = {"Authorization": "Bearer wrong-token"}
    assert client.get("/api/v1/incidents", headers=bad).status_code == 401
    assert client.get("/api/v1/applications").status_code == 401
    incident_id = three_incidents["old"]
    for suffix in ("", "/events", "/timeline", "/audit"):
        assert client.get(f"/api/v1/incidents/{incident_id}{suffix}").status_code == 401


def test_incidents_sorted_by_last_seen_desc(client, three_incidents):
    response = client.get("/api/v1/incidents", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    ids = [item["id"] for item in body["items"]]
    assert ids == [three_incidents["new"], three_incidents["mid"], three_incidents["old"]]
    assert body["total"] == 3
    last_seen = [item["last_seen"] for item in body["items"]]
    assert last_seen == sorted(last_seen, reverse=True)

    asc = client.get("/api/v1/incidents", params={"order": "asc"}, headers=AUTH).json()
    assert [i["id"] for i in asc["items"]] == list(reversed(ids))


def test_incidents_filter_by_status_severity_and_application(client, app, apps, three_incidents):
    with app.state.session_factory() as session:
        session.get(Incident, three_incidents["old"]).status = "resolved"
        session.commit()

    resolved = client.get("/api/v1/incidents", params={"status": "resolved"}, headers=AUTH).json()
    assert [i["id"] for i in resolved["items"]] == [three_incidents["old"]]
    assert resolved["total"] == 1

    opened = client.get("/api/v1/incidents", params={"status": "open"}, headers=AUTH).json()
    assert {i["id"] for i in opened["items"]} == {three_incidents["new"], three_incidents["mid"]}
    assert all(i["status"] == "open" for i in opened["items"])

    # KeyError in production -> critical; TypeError in development -> high.
    critical = client.get("/api/v1/incidents", params={"severity": "critical"}, headers=AUTH).json()
    assert [i["id"] for i in critical["items"]] == [three_incidents["mid"]]
    high = client.get("/api/v1/incidents", params={"severity": "high"}, headers=AUTH).json()
    assert [i["id"] for i in high["items"]] == [three_incidents["new"]]
    assert all(i["severity"] == "high" for i in high["items"])

    both = client.get(
        "/api/v1/incidents", params={"status": "open", "severity": "medium"}, headers=AUTH
    ).json()
    assert both["items"] == []

    blog = client.get(
        "/api/v1/incidents", params={"application_id": apps["demo-blog"]}, headers=AUTH
    ).json()
    assert [i["id"] for i in blog["items"]] == [three_incidents["mid"]]
    assert blog["items"][0]["application_name"] == "Demo Blog"


def test_incidents_invalid_filters_return_422(client, three_incidents):
    assert client.get("/api/v1/incidents", params={"status": "bogus"}, headers=AUTH).status_code == 422
    assert client.get("/api/v1/incidents", params={"severity": "huge"}, headers=AUTH).status_code == 422
    assert client.get("/api/v1/incidents", params={"limit": 0}, headers=AUTH).status_code == 422
    assert client.get("/api/v1/incidents", params={"offset": -1}, headers=AUTH).status_code == 422


def test_incidents_pagination(client, three_incidents):
    page = client.get("/api/v1/incidents", params={"limit": 2, "offset": 0}, headers=AUTH).json()
    assert [i["id"] for i in page["items"]] == [three_incidents["new"], three_incidents["mid"]]
    assert page["total"] == 3 and page["limit"] == 2 and page["offset"] == 0
    rest = client.get("/api/v1/incidents", params={"limit": 2, "offset": 2}, headers=AUTH).json()
    assert [i["id"] for i in rest["items"]] == [three_incidents["old"]]


def test_unknown_incident_returns_json_404(client, apps):
    for suffix in ("", "/events", "/timeline", "/audit"):
        response = client.get(f"/api/v1/incidents/999999{suffix}", headers=AUTH)
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")
        body = response.json()
        assert body["detail"] == "Incident not found"
        assert body["error"] == "not_found"


def test_unknown_route_returns_json_404(client):
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"] == "not_found"


def test_incident_detail(client, three_incidents):
    response = client.get(f"/api/v1/incidents/{three_incidents['mid']}", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == three_incidents["mid"]
    assert len(body["fingerprint"]) > 0
    assert body["event_count"] == 1
    assert body["severity"] == "critical"
    assert body["status"] == "open"
    assert body["exception_type"] == "KeyError"
    assert body["latest_rca_status"] is None
    assert body["latest_repair_status"] is None


def test_incident_events_are_redacted(client, apps):
    secret = "Bearer abcdefghijklmnopqrstuvwxyz0123456789"
    incident_id = _post(
        client, n=1, message=f"auth failed with {secret}",
        metadata={"password": "hunter2-fake-value", "path": "/orders"},
    )
    _post(client, n=2, timestamp="2024-03-01T10:05:00Z")
    response = client.get(f"/api/v1/incidents/{incident_id}/events", headers=AUTH)
    assert response.status_code == 200
    events = response.json()
    assert len(events) == 2
    assert [e["event_id"] for e in events] == ["evt-demo-shop-2", "evt-demo-shop-1"]
    first = events[1]
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in first["message"]
    assert REDACTED in first["message"]
    assert first["metadata"]["password"] == REDACTED
    assert first["metadata"]["path"] == "/orders"
    assert "hunter2" not in response.text


def test_timeline_for_two_events_is_ordered(client, clock, apps):
    incident_id = _post(client, n=1)
    clock.advance(minutes=5)
    _post(client, n=2, timestamp="2024-03-01T10:05:00Z")

    response = client.get(f"/api/v1/incidents/{incident_id}/timeline", headers=AUTH)
    assert response.status_code == 200
    entries = response.json()
    assert [e["type"] for e in entries] == [
        "event_received",
        "incident_grouped",
        "event_received",
        "incident_grouped",
    ]
    timestamps = [datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")) for e in entries]
    assert all(t is not None for t in timestamps)
    assert timestamps == sorted(timestamps)
    assert timestamps[2] - timestamps[0] == timedelta(minutes=5)
    assert entries[1]["details"]["new_incident"] is True
    assert entries[3]["details"]["new_incident"] is False
    assert entries[3]["details"]["event_count"] == 2


def test_timeline_includes_regression(client, app, clock, apps):
    incident_id = _post(client, n=1)
    with app.state.session_factory() as session:
        incident = session.get(Incident, incident_id)
        incident.status = "resolved"
        incident.resolved_at = datetime(2024, 3, 1, 12, 30, tzinfo=timezone.utc)
        session.commit()
    clock.advance(hours=1)
    _post(client, n=2, timestamp="2024-03-01T13:00:00Z")

    entries = client.get(f"/api/v1/incidents/{incident_id}/timeline", headers=AUTH).json()
    types = [e["type"] for e in entries]
    assert "incident_regressed" in types
    assert types[:2] == ["event_received", "incident_grouped"]


def test_audit_log_for_incident(client, apps):
    incident_id = _post(client, n=1)
    response = client.get(f"/api/v1/incidents/{incident_id}/audit", headers=AUTH)
    assert response.status_code == 200
    entries = response.json()
    assert [e["action"] for e in entries] == ["event_received", "incident_grouped"]
    assert all(e["actor"] == "ingest" for e in entries)
    assert all("created_at" in e for e in entries)


def test_applications_with_incident_counts(client, app, apps, three_incidents):
    with app.state.session_factory() as session:
        session.get(Incident, three_incidents["old"]).status = "resolved"
        session.commit()

    response = client.get("/api/v1/applications", headers=AUTH)
    assert response.status_code == 200
    by_slug = {a["slug"]: a for a in response.json()}
    assert set(by_slug) == {"demo-shop", "demo-blog"}
    shop = by_slug["demo-shop"]
    assert shop["incident_counts"]["open"] == 1
    assert shop["incident_counts"]["resolved"] == 1
    assert shop["incident_counts"]["regressed"] == 0
    assert shop["total_incidents"] == 2
    assert by_slug["demo-blog"]["incident_counts"]["open"] == 1
    assert by_slug["demo-blog"]["total_incidents"] == 1
    assert "ingest_key_hash" not in response.text
