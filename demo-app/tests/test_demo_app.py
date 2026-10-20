"""Normal demo app tests: they pass today and must keep passing after a correct fix."""

import ast
from pathlib import Path

import pytest

from demo_app.config import ConfigurationError, DemoSettings, load_settings
from demo_app.main import create_app

DEMO_ROOT = Path(__file__).resolve().parents[1]


def test_health(faulty_client):
    response = faulty_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "environment": "development",
        "faults_enabled": True,
    }


def test_profile_happy_path(faulty_client):
    response = faulty_client.get("/users/1/profile")
    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "ADA"
    assert body["email"] == "ada@example.test"


def test_profile_unknown_user_returns_404(faulty_client):
    assert faulty_client.get("/users/999/profile").status_code == 404


@pytest.mark.parametrize("quantity", [2, "2"])
def test_create_order_happy_path(faulty_client, quantity):
    response = faulty_client.post("/orders", json={"product": "gadget", "quantity": quantity})
    assert response.status_code == 201
    body = response.json()
    assert body["quantity"] == 2
    assert body["subtotal"] == 50.0
    total = faulty_client.get(f"/orders/{body['id']}/total")
    assert total.status_code == 200
    assert total.json()["total"] == 50.0


def test_create_order_unknown_product_returns_422(faulty_client):
    response = faulty_client.post("/orders", json={"product": "nothing", "quantity": 1})
    assert response.status_code == 422


@pytest.mark.parametrize(("coupon", "expected"), [(None, 30.0), ("SAVE10", 27.0), ("FIVEOFF", 25.0)])
def test_order_total_happy_path(faulty_client, coupon, expected):
    params = {"coupon": coupon} if coupon else {}
    response = faulty_client.get("/orders/1/total", params=params)
    assert response.status_code == 200
    assert response.json()["total"] == expected


def test_unknown_coupon_returns_422(faulty_client):
    assert faulty_client.get("/orders/1/total", params={"coupon": "NOPE"}).status_code == 422


def test_disabled_faults_follow_correct_behaviour(safe_client):
    assert safe_client.get("/health").json()["faults_enabled"] is False
    profile = safe_client.get("/users/2/profile")
    assert profile.status_code == 200
    assert profile.json()["display_name"] == "BRUNO SAMPLE"
    order = safe_client.post("/orders", json={"product": "widget", "quantity": "three"})
    assert order.status_code == 422
    total = safe_client.get("/orders/1/total", params={"coupon": "BIGDEAL"})
    assert total.status_code == 200
    assert total.json()["total"] == 0.0


def test_production_with_faults_enabled_raises(monkeypatch):
    monkeypatch.setenv("DEMO_ENVIRONMENT", "production")
    monkeypatch.setenv("DEMO_FAULTS_ENABLED", "true")
    with pytest.raises(ConfigurationError):
        create_app()


def test_production_settings_object_with_faults_raises():
    with pytest.raises(ConfigurationError):
        DemoSettings(environment="production", faults_enabled=True)


def test_production_defaults_to_faults_disabled():
    settings = load_settings({"DEMO_ENVIRONMENT": "production"})
    assert settings.faults_enabled is False


def test_development_defaults_to_faults_enabled():
    assert load_settings({}).faults_enabled is True


def test_invalid_boolean_raises():
    with pytest.raises(ConfigurationError):
        load_settings({"DEMO_FAULTS_ENABLED": "maybe"})


def test_demo_app_never_imports_backend():
    forbidden = {"app", "backend"}
    files = [p for p in DEMO_ROOT.rglob("*.py") if ".venv" not in p.parts]
    assert files
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                roots = {node.module.split(".")[0]}
            else:
                continue
            assert not roots & forbidden, f"{path} imports {roots & forbidden}"
