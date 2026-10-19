import pytest
from sqlalchemy import inspect, select

from app.config import Settings
from app.models import Application, AuditLog, AuthorizedRepository
from app.security.auth import hash_ingest_key

ADMIN_TOKEN = "fake-admin-token-for-tests"
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


@pytest.fixture
def settings(tmp_path, database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        workdir=str(tmp_path / "workdir"),
        admin_token=ADMIN_TOKEN,
    )


def _create_app(client, name="Demo Shop", **extra):
    return client.post("/api/v1/admin/applications", json={"name": name, **extra}, headers=AUTH)


def test_core_tables_exist(app):
    tables = set(inspect(app.state.engine).get_table_names())
    assert {
        "applications",
        "authorized_repositories",
        "incidents",
        "events",
        "audit_logs",
        "jobs",
        "rca_reports",
        "repair_attempts",
    }.issubset(tables)


def test_create_application_requires_token(client):
    assert client.post("/api/v1/admin/applications", json={"name": "X"}).status_code == 401
    wrong = client.post(
        "/api/v1/admin/applications",
        json={"name": "X"},
        headers={"Authorization": "Bearer not-the-token"},
    )
    assert wrong.status_code == 401


def test_create_application_returns_key_once_and_stores_only_hash(client, app):
    response = _create_app(client, budget_usd=12.5)
    assert response.status_code == 201
    body = response.json()
    key = body["ingest_key"]
    assert key
    assert body["name"] == "Demo Shop"
    assert body["slug"] == "demo-shop"
    assert body["budget_usd"] == 12.5
    assert "ingest_key_hash" not in body

    with app.state.session_factory() as session:
        stored = session.get(Application, body["id"])
        assert stored.ingest_key_hash == hash_ingest_key(key)
        assert stored.ingest_key_hash != key
        # The plaintext key appears nowhere in the stored row.
        row_values = [str(getattr(stored, c.key)) for c in Application.__table__.columns]
        assert all(key not in value for value in row_values)
        audit = session.scalars(select(AuditLog)).all()
        assert [a.action for a in audit] == ["application.created"]
        assert all(key not in str(a.details) for a in audit)


def test_list_applications_never_exposes_keys_or_hashes(client):
    key = _create_app(client).json()["ingest_key"]
    _create_app(client, name="Other App")

    response = client.get("/api/v1/admin/applications", headers=AUTH)
    assert response.status_code == 200
    items = response.json()
    assert [i["name"] for i in items] == ["Demo Shop", "Other App"]
    for item in items:
        assert "ingest_key" not in item
        assert "ingest_key_hash" not in item
    assert key not in response.text
    assert hash_ingest_key(key) not in response.text


def test_duplicate_application_name_returns_409(client):
    assert _create_app(client).status_code == 201
    duplicate = _create_app(client)
    assert duplicate.status_code == 409


def test_create_application_validates_name(client):
    assert _create_app(client, name="").status_code == 422


def test_register_repository_with_string_test_command_returns_422(client):
    app_id = _create_app(client).json()["id"]
    response = client.post(
        f"/api/v1/admin/applications/{app_id}/repositories",
        json={"name": "demo", "local_path": "/tmp/demo", "test_command": "python -m pytest -q"},
        headers=AUTH,
    )
    assert response.status_code == 422


@pytest.mark.parametrize("test_command", [[], [""], ["python", 3], None])
def test_register_repository_rejects_invalid_argv(client, test_command):
    app_id = _create_app(client).json()["id"]
    response = client.post(
        f"/api/v1/admin/applications/{app_id}/repositories",
        json={"name": "demo", "local_path": "/tmp/demo", "test_command": test_command},
        headers=AUTH,
    )
    assert response.status_code == 422


def test_register_repository_requires_a_source(client):
    app_id = _create_app(client).json()["id"]
    response = client.post(
        f"/api/v1/admin/applications/{app_id}/repositories",
        json={"name": "demo", "test_command": ["python", "-m", "pytest"]},
        headers=AUTH,
    )
    assert response.status_code == 422


def test_register_and_list_repositories(client, app):
    app_id = _create_app(client).json()["id"]
    argv = ["python", "-m", "pytest", "-q", "-m", "not fault_reproduction"]
    response = client.post(
        f"/api/v1/admin/applications/{app_id}/repositories",
        json={
            "name": "demo-app",
            "local_path": "/srv/fictitious/demo-app",
            "base_branch": "main",
            "test_command": argv,
            "github_repo": "example-org/demo-app",
        },
        headers=AUTH,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["application_id"] == app_id
    assert body["test_command"] == argv
    assert body["github_repo"] == "example-org/demo-app"

    listed = client.get(f"/api/v1/admin/applications/{app_id}/repositories", headers=AUTH)
    assert listed.status_code == 200
    assert [r["name"] for r in listed.json()] == ["demo-app"]
    assert listed.json()[0]["test_command"] == argv

    with app.state.session_factory() as session:
        repo = session.scalars(select(AuthorizedRepository)).one()
        assert repo.test_command == argv


def test_duplicate_repository_name_returns_409(client):
    app_id = _create_app(client).json()["id"]
    payload = {"name": "demo", "clone_url": "https://example.invalid/demo.git", "test_command": ["pytest"]}
    url = f"/api/v1/admin/applications/{app_id}/repositories"
    assert client.post(url, json=payload, headers=AUTH).status_code == 201
    assert client.post(url, json=payload, headers=AUTH).status_code == 409


def test_repositories_for_unknown_application_return_404(client):
    url = "/api/v1/admin/applications/999/repositories"
    assert client.get(url, headers=AUTH).status_code == 404
    response = client.post(
        url, json={"name": "x", "local_path": "/tmp/x", "test_command": ["pytest"]}, headers=AUTH
    )
    assert response.status_code == 404


def test_repository_endpoints_require_token(client):
    app_id = _create_app(client).json()["id"]
    url = f"/api/v1/admin/applications/{app_id}/repositories"
    assert client.get(url).status_code == 401
    assert client.post(f"/api/v1/admin/applications/{app_id}/rotate-key").status_code == 401
