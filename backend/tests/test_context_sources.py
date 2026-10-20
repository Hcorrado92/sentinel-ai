from datetime import datetime, timezone

import pytest

from app.context_sources import (
    ContextSource,
    EventsContextSource,
    MCPContextSource,
    OpenTelemetryContextSource,
    RepositoryContextSource,
    collect_available,
    default_context_sources,
)
from app.db import create_db_engine, create_session_factory, init_db
from app.models import Application, Event, Incident
from app.repo_tools import RepoReader

NOW = datetime(2024, 6, 1, 12, tzinfo=timezone.utc)
STACK = (
    "Traceback (most recent call last):\n"
    '  File "/srv/shop/shop/pricing.py", line 2, in total\n'
    "    return price * qty\n"
    "TypeError: unsupported operand type(s)"
)


@pytest.fixture
def db(database_url):
    engine = create_db_engine(database_url)
    init_db(engine)
    session = create_session_factory(engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "shop").mkdir(parents=True)
    (root / "shop" / "pricing.py").write_bytes(
        b"def total(price, qty):\n    return price * qty\nAPI_KEY = 'api_key=fictitious-repo-key-77'\n"
    )
    return root


@pytest.fixture
def incident(db):
    application = Application(name="Fictitious Shop", slug="fictitious-shop", ingest_key_hash="0" * 64)
    db.add(application)
    db.flush()
    incident = Incident(
        application_id=application.id,
        fingerprint="d" * 64,
        title="TypeError: unsupported operand type(s)",
        exception_type="TypeError",
        severity="medium",
        status="open",
        event_count=1,
        first_seen=NOW,
        last_seen=NOW,
    )
    db.add(incident)
    db.flush()
    db.add(
        Event(
            application_id=application.id,
            incident_id=incident.id,
            event_id="evt-ctx-1",
            timestamp=NOW,
            exception_type="TypeError",
            # Stored raw on purpose: the source redacts again.
            message="unsupported operand token=fictitious-ctx-token",
            stack_trace=STACK,
            environment="staging",
            metadata_json={"path": "/cart", "api_key": "fictitious-ctx-key"},
        )
    )
    db.commit()
    return incident


@pytest.mark.parametrize("source_class", [MCPContextSource, OpenTelemetryContextSource])
def test_placeholders_report_not_available_and_not_implemented(db, incident, source_class):
    source = source_class()
    assert isinstance(source, ContextSource)

    status = source.status()

    assert status.available is False
    assert source.available is False
    assert status.name == source.name
    assert "not implemented" in status.reason.lower()
    assert source.collect(db, incident) == []


def test_placeholder_names():
    assert MCPContextSource().name == "mcp"
    assert OpenTelemetryContextSource().name == "opentelemetry"


def test_events_source_is_available_and_redacts(db, incident):
    source = EventsContextSource()
    assert source.status().available is True

    [item] = source.collect(db, incident)

    assert item.source == "events"
    assert "unsupported operand" in item.content
    assert "pricing.py" in item.content
    assert "/cart" in item.content
    assert "fictitious-ctx-token" not in item.content
    assert "fictitious-ctx-key" not in item.content
    assert "[REDACTED]" in item.content


def test_repository_source_reads_stack_trace_files_redacted(db, incident, repo):
    source = RepositoryContextSource(RepoReader(repo))
    assert source.status().available is True

    items = source.collect(db, incident)

    assert [item.title.split(" ")[0] for item in items] == ["shop/pricing.py"]
    assert "return price * qty" in items[0].content
    assert "fictitious-repo-key-77" not in items[0].content


def test_repository_source_without_repository_is_unavailable(db, incident):
    source = RepositoryContextSource(None)
    status = source.status()
    assert status.available is False
    assert "repository" in status.reason
    assert source.collect(db, incident) == []


def test_default_sources_and_collect_skips_unavailable(db, incident, repo):
    sources = default_context_sources(RepoReader(repo))
    statuses = {source.name: source.status() for source in sources}

    assert set(statuses) == {"events", "repository", "mcp", "opentelemetry"}
    assert statuses["events"].available and statuses["repository"].available
    assert not statuses["mcp"].available and not statuses["opentelemetry"].available

    items = collect_available(sources, db, incident)
    assert {item.source for item in items} == {"events", "repository"}
