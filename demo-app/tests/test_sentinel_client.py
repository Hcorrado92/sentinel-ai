"""Tests for the Sentinel reporting client and the unhandled-exception handler.

Tests that trigger an intentional demo fault are marked ``fault_reproduction``
(they depend on the fault existing); the rest use a test-only failing route so
the reporting behaviour stays verified after the faults are repaired.
"""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from demo_app.config import DemoSettings
from demo_app.main import create_app
from demo_app.sentinel_client import SentinelReporter

SENTINEL_URL = "http://sentinel.example.test"
INGEST_KEY = "fake-ingest-key-for-tests"


class Recorder:
    def __init__(self, response_status: int = 202, error: Exception | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.response_status = response_status
        self.error = error

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.response_status, json={"event_id": "x", "incident_id": 1})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def _client(reporter: SentinelReporter, faults_enabled: bool = True) -> TestClient:
    app = create_app(
        DemoSettings(environment="development", faults_enabled=faults_enabled), reporter=reporter
    )

    def boom() -> None:
        raise RuntimeError("test-only failure")

    app.add_api_route("/__boom", boom, methods=["GET"])
    return TestClient(app, raise_server_exceptions=False)


def _reporter(recorder: Recorder, environ: dict[str, str] | None = None) -> SentinelReporter:
    env = {"SENTINEL_URL": SENTINEL_URL, "SENTINEL_INGEST_KEY": INGEST_KEY} if environ is None else environ
    return SentinelReporter.from_env(env, transport=recorder.transport)


@pytest.mark.fault_reproduction
def test_null_fault_sends_exactly_one_ingest_post():
    recorder = Recorder()
    client = _client(_reporter(recorder))

    response = client.get("/users/2/profile", headers={"X-Request-ID": "req-123"})

    assert response.status_code == 500
    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{SENTINEL_URL}/api/v1/ingest/events"
    assert request.headers["X-Sentinel-Key"] == INGEST_KEY
    body = json.loads(request.content)
    assert body["application"] == "demo-app"
    assert body["exception_type"] == "AttributeError"
    assert "Traceback" in body["stack_trace"]
    assert "_display_name" in body["stack_trace"]
    assert body["correlation_id"] == "req-123"
    assert body["environment"] == "development"
    assert body["metadata"] == {"method": "GET", "path": "/users/2/profile"}
    assert body["event_id"]
    assert body["timestamp"]


@pytest.mark.fault_reproduction
def test_null_fault_without_sentinel_url_makes_no_call():
    recorder = Recorder()
    client = _client(_reporter(recorder, {"SENTINEL_INGEST_KEY": INGEST_KEY}))

    response = client.get("/users/2/profile")

    assert response.status_code == 500
    assert recorder.requests == []


@pytest.mark.fault_reproduction
@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("connection refused"), httpx.ReadTimeout("timed out")],
)
def test_null_fault_with_failing_transport_still_returns_500(error):
    recorder = Recorder(error=error)
    client = _client(_reporter(recorder))

    response = client.get("/users/2/profile")

    assert response.status_code == 500
    assert len(recorder.requests) == 1


def test_unhandled_exception_is_reported_with_generated_correlation_id():
    recorder = Recorder()
    client = _client(_reporter(recorder))

    response = client.get("/__boom", params={"token": "should-not-leak"})

    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert len(recorder.requests) == 1
    body = json.loads(recorder.requests[0].content)
    assert body["exception_type"] == "RuntimeError"
    assert body["message"] == "test-only failure"
    assert body["correlation_id"]
    assert body["metadata"] == {"method": "GET", "path": "/__boom"}
    assert "should-not-leak" not in recorder.requests[0].content.decode()


def test_event_ids_are_unique_per_report():
    recorder = Recorder()
    client = _client(_reporter(recorder))

    client.get("/__boom")
    client.get("/__boom")

    ids = {json.loads(r.content)["event_id"] for r in recorder.requests}
    assert len(ids) == 2


@pytest.mark.parametrize(
    "environ",
    [{}, {"SENTINEL_INGEST_KEY": INGEST_KEY}, {"SENTINEL_URL": SENTINEL_URL}],
)
def test_reporter_disabled_without_full_configuration(environ):
    recorder = Recorder()
    reporter = _reporter(recorder, environ)
    client = _client(reporter)

    response = client.get("/__boom")

    assert reporter.enabled is False
    assert response.status_code == 500
    assert recorder.requests == []


@pytest.mark.parametrize(
    "recorder",
    [
        Recorder(error=httpx.ConnectError("connection refused")),
        Recorder(error=httpx.ConnectTimeout("timed out")),
        Recorder(error=httpx.ReadTimeout("timed out")),
        Recorder(response_status=503),
        Recorder(response_status=401),
    ],
)
def test_reporting_failures_never_change_the_response(recorder, caplog):
    client = _client(_reporter(recorder))

    response = client.get("/__boom")

    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert len(recorder.requests) == 1
    assert "Sentinel" in caplog.text


def test_handled_errors_are_not_reported():
    recorder = Recorder()
    client = _client(_reporter(recorder))

    assert client.get("/users/999/profile").status_code == 404
    assert client.get("/health").status_code == 200
    assert recorder.requests == []


def test_send_returns_false_on_failure_and_true_on_success():
    ok = SentinelReporter(SENTINEL_URL, INGEST_KEY, transport=Recorder().transport)
    failing = SentinelReporter(
        SENTINEL_URL, INGEST_KEY, transport=Recorder(error=httpx.ConnectError("x")).transport
    )
    payload = ok.build_payload(ValueError("bad"), method="GET", path="/x?secret=1")

    assert payload["metadata"]["path"] == "/x"
    assert ok.send(payload) is True
    assert failing.send(payload) is False
