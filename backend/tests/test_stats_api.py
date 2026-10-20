from datetime import datetime, timezone

import pytest

from app.api.stats import MAX_BUCKETS, bucket_counts
from app.clock import FakeClock
from app.config import Settings
from app.main import create_app
from app.models import Application
from app.security.auth import hash_ingest_key

ADMIN_TOKEN = "fake-admin-token-for-tests"
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
INGEST_URL = "/api/v1/ingest/events"
KEY_SHOP = "sk_sentinel_fake-stats-key-shop"
KEY_BLOG = "sk_sentinel_fake-stats-key-blog"
KEYS = {"demo-shop": KEY_SHOP, "demo-blog": KEY_BLOG}

TRACE = (
    "Traceback (most recent call last):\n"
    '  File "/srv/app/shop/orders.py", line 12, in {function}\n'
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


def _post(client, *, n: int, timestamp: str, application="demo-shop",
          function="create_order", exc="TypeError", environment="development"):
    event = {
        "application": application,
        "event_id": f"evt-stats-{application}-{n}",
        "timestamp": timestamp,
        "exception_type": exc,
        "message": f"failure {n}",
        "stack_trace": TRACE.format(function=function, exc=exc),
        "environment": environment,
    }
    response = client.post(INGEST_URL, json=event, headers={"X-Sentinel-Key": KEYS[application]})
    assert response.status_code == 202, response.text
    return response.json()["incident_id"]


def _start(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _buckets(body: dict) -> list[tuple[datetime, int]]:
    return [(_start(b["start"]), b["count"]) for b in body["buckets"]]


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# --- stats endpoint ---------------------------------------------------------


def test_incident_stats_hourly_bucket_counts(client, apps):
    incident_id = _post(client, n=1, timestamp="2024-03-01T10:05:00Z")
    _post(client, n=2, timestamp="2024-03-01T10:59:59Z")
    _post(client, n=3, timestamp="2024-03-01T12:30:00Z")
    _post(client, n=4, timestamp="2024-03-01T12:00:00Z")
    _post(client, n=5, timestamp="2024-03-01T12:45:00Z")
    # Another incident (different function) must not be counted.
    other = _post(client, n=6, function="other_fn", timestamp="2024-03-01T10:10:00Z")
    assert other != incident_id

    response = client.get(f"/api/v1/incidents/{incident_id}/stats", headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["bucket"] == "hour"
    assert body["incident_id"] == incident_id
    assert body["total"] == 5
    # 11:00 has no events and is zero-filled.
    assert _buckets(body) == [
        (utc(2024, 3, 1, 10), 2),
        (utc(2024, 3, 1, 11), 0),
        (utc(2024, 3, 1, 12), 3),
    ]


def test_incident_stats_daily_buckets_and_dedup(client, apps):
    incident_id = _post(client, n=1, timestamp="2024-03-01T23:59:00Z")
    _post(client, n=2, timestamp="2024-03-03T00:00:00Z")
    _post(client, n=3, timestamp="2024-03-03T18:00:00Z")
    # A repeated event id is deduplicated and not counted twice.
    _post(client, n=3, timestamp="2024-03-03T18:00:00Z")

    body = client.get(
        f"/api/v1/incidents/{incident_id}/stats", params={"bucket": "day"}, headers=AUTH
    ).json()
    assert body["bucket"] == "day"
    assert body["total"] == 3
    assert _buckets(body) == [
        (utc(2024, 3, 1), 1),
        (utc(2024, 3, 2), 0),
        (utc(2024, 3, 3), 2),
    ]


def test_global_event_stats_filter_by_application(client, apps):
    _post(client, n=1, timestamp="2024-03-01T08:15:00Z")
    _post(client, n=2, function="other_fn", timestamp="2024-03-01T09:15:00Z")
    _post(client, n=3, application="demo-blog", timestamp="2024-03-01T09:45:00Z")

    everything = client.get("/api/v1/stats/events", params={"bucket": "hour"}, headers=AUTH).json()
    assert everything["total"] == 3
    assert _buckets(everything) == [(utc(2024, 3, 1, 8), 1), (utc(2024, 3, 1, 9), 2)]

    shop = client.get(
        "/api/v1/stats/events", params={"application_id": apps["demo-shop"]}, headers=AUTH
    ).json()
    assert shop["application_id"] == apps["demo-shop"]
    assert _buckets(shop) == [(utc(2024, 3, 1, 8), 1), (utc(2024, 3, 1, 9), 1)]


def test_stats_empty_unknown_invalid_and_auth(client, apps):
    empty = client.get("/api/v1/stats/events", headers=AUTH).json()
    assert empty["buckets"] == [] and empty["total"] == 0

    assert client.get("/api/v1/incidents/999999/stats", headers=AUTH).status_code == 404
    assert client.get("/api/v1/stats/events", params={"bucket": "minute"}, headers=AUTH).status_code == 422
    assert client.get("/api/v1/stats/events").status_code == 401
    incident_id = _post(client, n=1, timestamp="2024-03-01T08:15:00Z")
    assert client.get(f"/api/v1/incidents/{incident_id}/stats").status_code == 401


def test_bucket_counts_handles_naive_timestamps_and_caps_buckets():
    naive = [datetime(2024, 3, 1, 10, 30), datetime(2024, 3, 1, 10, 31)]
    assert bucket_counts(naive, "hour") == [{"start": utc(2024, 3, 1, 10), "count": 2}]

    spread = [utc(2024, 1, 1), utc(2024, 12, 31)]
    capped = bucket_counts(spread, "hour")
    assert len(capped) == MAX_BUCKETS
    assert capped[-1] == {"start": utc(2024, 12, 31), "count": 1}


# --- incident filters -------------------------------------------------------


def test_incident_list_search_and_active_filter(client, apps):
    type_error = _post(client, n=1, timestamp="2024-03-01T08:00:00Z", exc="TypeError")
    key_error = _post(client, n=2, function="f2", timestamp="2024-03-01T09:00:00Z", exc="KeyError")
    under = _post(client, n=3, function="f3", timestamp="2024-03-01T10:00:00Z", exc="Custom_Error")
    assert client.post(f"/api/v1/incidents/{key_error}/resolve", headers=AUTH).status_code == 200

    found = client.get("/api/v1/incidents", params={"q": "typeerr"}, headers=AUTH).json()
    assert [i["id"] for i in found["items"]] == [type_error]

    # LIKE wildcards are matched literally.
    literal = client.get("/api/v1/incidents", params={"q": "m_e"}, headers=AUTH).json()
    assert [i["id"] for i in literal["items"]] == [under]
    assert client.get("/api/v1/incidents", params={"q": "%"}, headers=AUTH).json()["items"] == []

    active = client.get("/api/v1/incidents", params={"status": "active"}, headers=AUTH).json()
    assert {i["id"] for i in active["items"]} == {type_error, under}
    resolved = client.get("/api/v1/incidents", params={"status": "resolved"}, headers=AUTH).json()
    assert [i["id"] for i in resolved["items"]] == [key_error]


# --- resolve ----------------------------------------------------------------


def test_resolve_writes_audit_and_later_event_regresses(client, clock, apps):
    incident_id = _post(client, n=1, timestamp="2024-03-01T10:00:00Z")
    clock.advance(minutes=30)

    response = client.post(f"/api/v1/incidents/{incident_id}/resolve", headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "resolved"
    assert _start(body["resolved_at"]) == utc(2024, 3, 1, 12, 30)

    detail = client.get(f"/api/v1/incidents/{incident_id}", headers=AUTH).json()
    assert detail["status"] == "resolved"

    audit = client.get(f"/api/v1/incidents/{incident_id}/audit", headers=AUTH).json()
    resolved = [e for e in audit if e["action"] == "incident_resolved"]
    assert len(resolved) == 1
    assert resolved[0]["actor"] == "admin"
    assert resolved[0]["details"]["previous_status"] == "open"
    timeline = client.get(f"/api/v1/incidents/{incident_id}/timeline", headers=AUTH).json()
    assert "incident_resolved" in [e["type"] for e in timeline]

    # Resolving twice is a conflict.
    assert client.post(f"/api/v1/incidents/{incident_id}/resolve", headers=AUTH).status_code == 409

    clock.advance(hours=1)
    assert _post(client, n=2, timestamp="2024-03-01T13:30:00Z") == incident_id
    regressed = client.get(f"/api/v1/incidents/{incident_id}", headers=AUTH).json()
    assert regressed["status"] == "regressed"
    assert regressed["resolved_at"] is None
    types = [e["type"] for e in client.get(
        f"/api/v1/incidents/{incident_id}/timeline", headers=AUTH
    ).json()]
    assert types.index("incident_resolved") < types.index("incident_regressed")


def test_resolve_requires_admin_and_existing_incident(client, apps):
    incident_id = _post(client, n=1, timestamp="2024-03-01T10:00:00Z")
    assert client.post(f"/api/v1/incidents/{incident_id}/resolve").status_code == 401
    assert client.get(f"/api/v1/incidents/{incident_id}", headers=AUTH).json()["status"] == "open"
    assert client.post("/api/v1/incidents/999999/resolve", headers=AUTH).status_code == 404
