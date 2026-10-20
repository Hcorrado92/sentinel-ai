import pytest
from fastapi.testclient import TestClient

from demo_app.config import DemoSettings
from demo_app.main import create_app


@pytest.fixture(autouse=True)
def _no_sentinel_env(monkeypatch):
    # Keep tests hermetic: never report to a real Sentinel from the environment.
    monkeypatch.delenv("SENTINEL_URL", raising=False)
    monkeypatch.delenv("SENTINEL_INGEST_KEY", raising=False)


@pytest.fixture
def faulty_client() -> TestClient:
    app = create_app(DemoSettings(environment="development", faults_enabled=True))
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def safe_client() -> TestClient:
    app = create_app(DemoSettings(environment="staging", faults_enabled=False))
    return TestClient(app, raise_server_exceptions=False)
