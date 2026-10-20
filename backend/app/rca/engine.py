"""RCA engine: gathers incident context, queries the AI provider and persists the report.

``run_rca`` maps the stack trace to files of the authorized repository through
the read-only ``RepoReader`` (T10), builds an injection-safe prompt, validates
the provider output as ``RCAReportModel`` (which downgrades any "confirmed"
hypothesis) and stores the redacted report as an ``RCAReport`` with status
``completed`` or ``failed``. When the application's AI budget is already
spent the provider is not called and the report fails with
``budget_exhausted``; reported costs are added to the application's spend.
Audit/timeline entries ``rca_started`` and ``rca_completed`` / ``rca_failed``
are recorded.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.ai.base import AIProvider
from app.ai.prompts import RepoExcerpt, build_rca_prompt
from app.clock import Clock
from app.models import Application, AuditLog, Event, Incident, RCAReport
from app.rca.schemas import RCAReportModel
from app.repo_tools import RepoAccessError, RepoReader
from app.security.redaction import redact_obj, redact_text

RCA_ACTOR = "rca"
EXCERPT_CONTEXT_LINES = 30
MAX_REPO_FILES = 5
MAX_ERROR_CHARS = 2000

_PY_FRAME = re.compile(r'File "(?P<path>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>[^\s]+))?')
_JS_FRAME = re.compile(
    r"at (?:(?P<func>[^\s(]+) )?\(?(?P<path>[^\s():]+(?::\\[^\s():]+)?):(?P<line>\d+)(?::\d+)?\)?"
)


@dataclass(frozen=True)
class Frame:
    path: str
    line: int | None
    function: str | None


def parse_frames(stack_trace: str) -> list[Frame]:
    """Return stack frames ordered innermost first."""
    python = [
        Frame(m.group("path"), int(m.group("line")), m.group("func"))
        for m in _PY_FRAME.finditer(stack_trace or "")
    ]
    if python:
        # Python tracebacks list the most recent call last.
        return list(reversed(python))
    return [
        Frame(m.group("path"), int(m.group("line")), m.group("func"))
        for m in _JS_FRAME.finditer(stack_trace or "")
    ]


def _repo_path(reader: RepoReader, frame: Frame) -> str | None:
    if frame.path.startswith("<") or "site-packages" in frame.path:
        return None
    found = reader.find_relevant_files(f'File "{frame.path}", line {frame.line or 1}')
    return found[0] if found else None


def gather_context(
    reader: RepoReader | None, stack_trace: str
) -> tuple[list[RepoExcerpt], dict | None]:
    """Map frames to repository files; return excerpts and the top repo frame."""
    if reader is None:
        return [], None
    located: dict[str, Frame] = {}
    top_frame: dict | None = None
    for frame in parse_frames(stack_trace):
        try:
            path = _repo_path(reader, frame)
        except RepoAccessError:
            continue
        if path is None:
            continue
        if top_frame is None:
            top_frame = {"file": path, "function": frame.function, "line": frame.line}
        located.setdefault(path, frame)
        if len(located) >= MAX_REPO_FILES:
            break

    excerpts: list[RepoExcerpt] = []
    for path, frame in located.items():
        start = 1
        end = None
        if frame.line:
            start = max(frame.line - EXCERPT_CONTEXT_LINES, 1)
            end = frame.line + EXCERPT_CONTEXT_LINES
        try:
            content = reader.read_file(path, start_line=start, end_line=end)
        except RepoAccessError:
            continue
        excerpts.append(RepoExcerpt(path=path, content=content, start_line=start))
    return excerpts, top_frame


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _audit(
    db: Session, clock: Clock, incident: Incident, action: str, details: dict
) -> None:
    db.add(
        AuditLog(
            application_id=incident.application_id,
            incident_id=incident.id,
            actor=RCA_ACTOR,
            action=action,
            details=details,
            created_at=clock.now(),
        )
    )


class BudgetExhausted(Exception):
    """The application's AI budget is spent: no provider call is made."""

    code = "budget_exhausted"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.code}: {detail}")


def budget_exhausted(application: Application | None) -> bool:
    return (
        application is not None
        and application.budget_usd is not None
        and (application.monthly_spend_usd or 0.0) >= application.budget_usd
    )


def _error_reason(exc: BaseException) -> str:
    reason = f"{type(exc).__name__}: {exc}"
    return redact_text(reason)[:MAX_ERROR_CHARS]


def run_rca(
    db: Session,
    incident: Incident,
    events: Sequence[Event],
    repo_reader: RepoReader | None,
    provider: AIProvider,
    clock: Clock,
) -> RCAReport:
    """Run the RCA for ``incident`` and return the persisted ``RCAReport``."""
    started = clock.now()
    report = RCAReport(
        incident_id=incident.id,
        status="running",
        provider=provider.name,
        created_at=started,
    )
    db.add(report)
    db.flush()
    _audit(db, clock, incident, "rca_started", {"rca_report_id": report.id, "provider": provider.name})
    db.commit()

    application = (
        db.get(Application, incident.application_id)
        if incident.application_id is not None
        else None
    )
    try:
        if budget_exhausted(application):
            # The provider is never called once the application's budget is spent.
            raise BudgetExhausted(
                f"application budget of {application.budget_usd} USD is exhausted"
            )
        ordered = sorted(events, key=lambda e: _as_utc(e.timestamp))
        stack_trace = ordered[-1].stack_trace if ordered else ""
        excerpts, top_frame = gather_context(repo_reader, stack_trace)
        prompt = build_rca_prompt(incident, ordered, excerpts, top_frame)
        result = provider.generate_structured(prompt, RCAReportModel)
        if application is not None and result.usage.cost_usd is not None:
            application.monthly_spend_usd = (
                application.monthly_spend_usd or 0.0
            ) + result.usage.cost_usd
        # Re-validate: the schema downgrades any "confirmed" status from the model.
        validated = RCAReportModel.model_validate(result.output.model_dump(mode="json"))
        # The model output is untrusted: it is redacted before it is stored.
        stored = redact_obj(validated.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001 - any provider failure is recorded
        finished = clock.now()
        report.status = "failed"
        report.error = _error_reason(exc)
        report.finished_at = finished
        report.duration_ms = _duration_ms(started, finished)
        _audit(
            db,
            clock,
            incident,
            "rca_failed",
            {"rca_report_id": report.id, "provider": provider.name, "error": report.error},
        )
        db.commit()
        return report

    finished = clock.now()
    report.status = "completed"
    report.summary = stored["summary"]
    report.report = stored
    report.provider = result.provider
    report.model = result.model
    report.input_tokens = result.usage.input_tokens
    report.output_tokens = result.usage.output_tokens
    report.cost_usd = result.usage.cost_usd
    report.finished_at = finished
    report.duration_ms = _duration_ms(started, finished)
    _audit(
        db,
        clock,
        incident,
        "rca_completed",
        {
            "rca_report_id": report.id,
            "provider": result.provider,
            "hypotheses": len(validated.hypotheses),
            "duration_ms": report.duration_ms,
            "cost_usd": report.cost_usd,
        },
    )
    db.commit()
    return report


def _duration_ms(started: datetime, finished: datetime) -> int:
    return max(int((_as_utc(finished) - _as_utc(started)).total_seconds() * 1000), 0)
