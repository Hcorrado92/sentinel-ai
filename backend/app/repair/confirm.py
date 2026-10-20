"""Link a test-verified repair to the RCA hypothesis it backs up.

When a repair attempt succeeds, the files changed by its diff are compared
with the RCA's suspect locations. The hypothesis that refers to a patched
suspect file (by path, file name or function name) is marked ``confirmed``
with a reference to the verified attempt; the highest-confidence match wins.
Nothing is confirmed when no patched file is a suspect location or no
hypothesis refers to one: a failed or unrelated repair leaves every hypothesis
as ``hypothesis``. The stored report is updated in place (as JSON), so the
model-facing schema, which never accepts ``confirmed``, stays unchanged.
"""

import copy
import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditLog, Incident, RCAReport, RepairAttempt
from app.repair.patch import PatchRejected, diff_paths

CONFIRM_ACTOR = "repair"


def _norm(path: str) -> str:
    path = (path or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.strip("/").lower()


def _same_file(suspect: str, patched: str) -> bool:
    a, b = _norm(suspect), _norm(patched)
    if not a or not b:
        return False
    # Suspect paths may carry a deployment prefix (e.g. /srv/app/...).
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def patched_files(diff: str | None) -> list[str]:
    if not diff or not diff.strip():
        return []
    try:
        paths = diff_paths(diff)
    except PatchRejected:
        return []
    return list(dict.fromkeys(paths))


def _hypothesis_text(hypothesis: dict[str, Any]) -> str:
    evidence = hypothesis.get("evidence") or []
    parts = [str(hypothesis.get("title") or ""), str(hypothesis.get("description") or "")]
    parts.extend(str(item) for item in evidence if isinstance(item, (str, int, float)))
    return "\n".join(parts).replace("\\", "/").lower()


def _refers_to(text: str, file: str, function: str | None) -> bool:
    norm = _norm(file)
    name = PurePosixPath(norm).name
    if norm and norm in text:
        return True
    if name and re.search(rf"(?<![\w.]){re.escape(name)}(?![\w])", text):
        return True
    return bool(function) and re.search(rf"\b{re.escape(function.lower())}\b", text) is not None


def find_confirmed_hypothesis(
    report: dict[str, Any], attempt: RepairAttempt
) -> tuple[int, str] | None:
    """Index of the hypothesis backed by ``attempt`` and the matched suspect file."""
    patched = patched_files(attempt.diff)
    suspects = [
        loc
        for loc in report.get("suspect_locations") or []
        if isinstance(loc, dict)
        and loc.get("file")
        and any(_same_file(str(loc["file"]), path) for path in patched)
    ]
    best: tuple[float, int, str] | None = None
    for index, hypothesis in enumerate(report.get("hypotheses") or []):
        if not isinstance(hypothesis, dict):
            continue
        text = _hypothesis_text(hypothesis)
        for loc in suspects:
            function = loc.get("function")
            if _refers_to(text, str(loc["file"]), str(function) if function else None):
                confidence = float(hypothesis.get("confidence") or 0.0)
                if best is None or confidence > best[0]:
                    best = (confidence, index, str(loc["file"]))
                break
    return (best[1], best[2]) if best else None


def confirm_from_repair(
    db: Session, rca: RCAReport, attempt: RepairAttempt, now: datetime
) -> int | None:
    """Mark the hypothesis backed by a verified ``attempt`` as confirmed.

    Returns the hypothesis index, or ``None`` when nothing was confirmed.
    """
    if attempt.status != "succeeded" or not attempt.tests_passed:
        return None
    report = copy.deepcopy(rca.report or {})
    match = find_confirmed_hypothesis(report, attempt)
    if match is None:
        return None
    index, file = match
    hypothesis = report["hypotheses"][index]
    hypothesis["status"] = "confirmed"
    hypothesis["confirmed_by"] = {
        "repair_attempt_id": attempt.id,
        "job_id": attempt.job_id,
        "file": file,
        "commit_sha": attempt.commit_sha,
    }
    rca.report = report
    incident = db.get(Incident, rca.incident_id)
    db.add(
        AuditLog(
            application_id=incident.application_id if incident else None,
            incident_id=rca.incident_id,
            actor=CONFIRM_ACTOR,
            action="rca_hypothesis_confirmed",
            details={
                "rca_report_id": rca.id,
                "hypothesis": index,
                "title": hypothesis.get("title"),
                "repair_attempt_id": attempt.id,
                "file": file,
            },
            created_at=now,
        )
    )
    return index
