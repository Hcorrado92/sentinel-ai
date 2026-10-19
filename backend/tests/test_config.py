from app.config import Settings
from app.db import create_db_engine


def test_defaults_are_safe():
    settings = Settings()

    assert settings.database_url == "sqlite:///./sentinel.db"
    assert settings.admin_token is None
    assert settings.ai_provider == "mock"
    assert settings.ai_provider_is_mock is True
    assert settings.github_publish is False
    assert settings.max_repair_attempts == 3
    assert settings.job_timeout_seconds > 0
    assert settings.workdir
    assert settings.max_body_bytes > 0
    assert settings.max_message_length > 0
    assert settings.max_stack_trace_length > 0
    assert settings.max_metadata_items > 0
    assert settings.max_metadata_bytes > 0


def test_reads_sentinel_prefixed_environment(monkeypatch, tmp_path):
    url = f"sqlite:///{(tmp_path / 'env.db').as_posix()}"
    monkeypatch.setenv("SENTINEL_DATABASE_URL", url)
    monkeypatch.setenv("SENTINEL_GITHUB_PUBLISH", "true")
    monkeypatch.setenv("SENTINEL_MAX_REPAIR_ATTEMPTS", "5")
    monkeypatch.setenv("SENTINEL_AI_PROVIDER", "anthropic")

    settings = Settings()

    assert settings.database_url == url
    assert settings.github_publish is True
    assert settings.max_repair_attempts == 5
    assert settings.ai_provider_is_mock is False


def test_unprefixed_variables_are_ignored(monkeypatch):
    monkeypatch.setenv("GITHUB_PUBLISH", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql://example/ignored")

    settings = Settings()

    assert settings.github_publish is False
    assert settings.database_url == "sqlite:///./sentinel.db"


def test_sqlite_engine_connects_to_configured_file(tmp_path):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'x.db').as_posix()}")
    try:
        with engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT 1").scalar() == 1
    finally:
        engine.dispose()
