import os
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture(autouse=True)
def _clean_sentinel_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make tests independent of any SENTINEL_* variable in the environment."""
    for name in list(os.environ):
        if name.upper().startswith("SENTINEL_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def database_url(tmp_path) -> str:
    return f"sqlite:///{(tmp_path / 'sentinel-test.db').as_posix()}"


@pytest.fixture
def settings(tmp_path, database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        workdir=str(tmp_path / "workdir"),
    )


@pytest.fixture
def app(settings: Settings) -> Iterator[FastAPI]:
    application = create_app(settings)
    yield application
    application.state.engine.dispose()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
