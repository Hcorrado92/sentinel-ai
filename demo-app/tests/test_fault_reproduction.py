"""Reproduction tests: they assert the intentional faults (status 500).

They pass while the faults exist and are excluded from Sentinel's repair
verification (``-m "not fault_reproduction"``).
"""

import pytest

pytestmark = pytest.mark.fault_reproduction


def test_profile_with_missing_nickname_returns_500(faulty_client):
    response = faulty_client.get("/users/2/profile")
    assert response.status_code == 500


def test_order_with_non_numeric_quantity_returns_500(faulty_client):
    response = faulty_client.post("/orders", json={"product": "widget", "quantity": "three"})
    assert response.status_code == 500


def test_total_with_oversized_coupon_returns_500(faulty_client):
    response = faulty_client.get("/orders/1/total", params={"coupon": "BIGDEAL"})
    assert response.status_code == 500


def test_faults_enabled_from_environment_reproduce(monkeypatch):
    from fastapi.testclient import TestClient

    from demo_app.main import create_app

    monkeypatch.setenv("DEMO_ENVIRONMENT", "development")
    monkeypatch.setenv("DEMO_FAULTS_ENABLED", "true")
    client = TestClient(create_app(), raise_server_exceptions=False)
    assert client.get("/users/2/profile").status_code == 500
