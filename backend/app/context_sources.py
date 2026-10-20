"""Context sources: where the RCA engine gathers evidence about an incident.

``ContextSource`` is the interface: ``status()`` tells whether the source can
be used (``available`` plus a ``reason`` when it cannot) and ``collect()``
returns redacted ``ContextItem`` objects for an incident. Everything a source
returns is untrusted data and must be placed inside ``<untrusted_data>``
blocks when it reaches a prompt (``app.ai.prompts``).

Built-in implementations:

- ``EventsContextSource``: the incident's most recent events (message, stack
  trace, environment and metadata), already redacted at ingestion and
  redacted again here.
- ``RepositoryContextSource``: excerpts of the authorized repository files
  that appear in the latest stack trace, read through the sandboxed read-only
  ``RepoReader``. Unavailable when the application has no readable
  authorized repository.

Documented placeholders (not implemented in the MVP; they always report
``available=False`` with a "not implemented" reason and collect nothing):

- ``MCPContextSource``: would query Model Context Protocol servers (for example
  ticketing or runbook tools) registered by an administrator, with the same
  allow-list, size limits and redaction as the repository tools.
- ``OpenTelemetryContextSource``: would read traces, spans and logs correlated
  with the event's ``correlation_id`` from an OpenTelemetry backend.
"""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Event, Incident
from app.rca.engine import gather_context
from app.repo_tools import RepoReader
from app.security.redaction import redact_obj, redact_text

DEFAULT_MAX_EVENTS = 5
NOT_IMPLEMENTED = "not implemented"


@dataclass(frozen=True)
class ContextSourceStatus:
    name: str
    available: bool
    reason: str | None = None


@dataclass(frozen=True)
class ContextItem:
    """A piece of (redacted, untrusted) evidence."""

    source: str
    title: str
    content: str


class ContextSource(ABC):
    name: str = "context"

    @abstractmethod
    def status(self) -> ContextSourceStatus:
        """Whether the source can be used, and why not when it cannot."""

    @abstractmethod
    def collect(self, db: Session, incident: Incident) -> list[ContextItem]:
        """Return the evidence for ``incident`` (empty when unavailable)."""

    @property
    def available(self) -> bool:
        return self.status().available


def _latest_events(db: Session, incident: Incident, limit: int) -> list[Event]:
    return list(
        db.scalars(
            select(Event)
            .where(Event.incident_id == incident.id)
            .order_by(Event.timestamp.desc(), Event.id.desc())
            .limit(limit)
        ).all()
    )


class EventsContextSource(ContextSource):
    name = "events"

    def __init__(self, max_events: int = DEFAULT_MAX_EVENTS) -> None:
        self.max_events = max_events

    def status(self) -> ContextSourceStatus:
        return ContextSourceStatus(self.name, True)

    def collect(self, db: Session, incident: Incident) -> list[ContextItem]:
        items = []
        for event in _latest_events(db, incident, self.max_events):
            metadata = json.dumps(redact_obj(event.metadata_json or {}), sort_keys=True, default=str)
            content = "\n".join(
                [
                    f"timestamp: {event.timestamp.isoformat() if event.timestamp else ''}",
                    f"environment: {event.environment}",
                    f"exception_type: {event.exception_type}",
                    "message:",
                    redact_text(event.message or ""),
                    "stack_trace:",
                    redact_text(event.stack_trace or ""),
                    "metadata:",
                    metadata,
                ]
            )
            items.append(ContextItem(self.name, f"event {event.event_id or event.id}", content))
        return items


class RepositoryContextSource(ContextSource):
    name = "repository"

    def __init__(self, reader: RepoReader | None) -> None:
        self.reader = reader

    def status(self) -> ContextSourceStatus:
        if self.reader is None:
            return ContextSourceStatus(
                self.name, False, "the application has no readable authorized repository"
            )
        return ContextSourceStatus(self.name, True)

    def collect(self, db: Session, incident: Incident) -> list[ContextItem]:
        if self.reader is None:
            return []
        latest = _latest_events(db, incident, 1)
        stack_trace = latest[0].stack_trace if latest else ""
        excerpts, _ = gather_context(self.reader, stack_trace or "")
        return [
            # RepoReader already redacts file contents.
            ContextItem(self.name, f"{excerpt.path} (from line {excerpt.start_line})", excerpt.content)
            for excerpt in excerpts
        ]


class _NotImplementedSource(ContextSource):
    detail = ""

    def status(self) -> ContextSourceStatus:
        return ContextSourceStatus(self.name, False, f"{NOT_IMPLEMENTED}: {self.detail}")

    def collect(self, db: Session, incident: Incident) -> list[ContextItem]:
        return []


class MCPContextSource(_NotImplementedSource):
    """Placeholder for Model Context Protocol servers (not available in the MVP)."""

    name = "mcp"
    detail = "MCP context servers are not available in this version of Sentinel AI"


class OpenTelemetryContextSource(_NotImplementedSource):
    """Placeholder for OpenTelemetry traces and logs (not available in the MVP)."""

    name = "opentelemetry"
    detail = "OpenTelemetry traces and logs are not available in this version of Sentinel AI"


def default_context_sources(reader: RepoReader | None) -> list[ContextSource]:
    """The built-in sources followed by the documented placeholders."""
    return [
        EventsContextSource(),
        RepositoryContextSource(reader),
        MCPContextSource(),
        OpenTelemetryContextSource(),
    ]


def collect_available(
    sources: list[ContextSource], db: Session, incident: Incident
) -> list[ContextItem]:
    """Collect from every available source; unavailable ones are skipped."""
    items: list[ContextItem] = []
    for source in sources:
        if source.status().available:
            items.extend(source.collect(db, incident))
    return items
