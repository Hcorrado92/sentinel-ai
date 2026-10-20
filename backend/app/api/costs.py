"""AI cost summary per application.

``GET /costs`` totals the tokens and USD reported by the AI provider for the
RCA reports and repair attempts of each application. A total is ``None``
("not available") when no run reported it, e.g. when only the deterministic
mock was used. Requires the admin bearer token.
"""

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Application, Incident, RCAReport, RepairAttempt
from app.security.auth import require_admin

_TOTALS = ("input_tokens", "output_tokens", "cost_usd")


class ApplicationCost(BaseModel):
    application_id: int
    application_name: str
    rca_runs: int
    repair_attempts: int
    # Runs that did not report a cost (mock or a provider without pricing).
    runs_without_cost: int
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None


class CostSummary(BaseModel):
    items: list[ApplicationCost]
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None


def _add(current: Any, value: Any) -> Any:
    if value is None:
        return current
    return value if current is None else current + value


def costs_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_admin)])

    @router.get("/costs", response_model=CostSummary)
    def cost_summary(db: Session = Depends(get_db)) -> dict:
        applications = db.scalars(select(Application).order_by(Application.id)).all()
        rows: dict[int, dict[str, Any]] = {
            application.id: {
                "application_id": application.id,
                "application_name": application.name,
                "rca_runs": 0,
                "repair_attempts": 0,
                "runs_without_cost": 0,
                **{key: None for key in _TOTALS},
            }
            for application in applications
        }
        for model, counter in ((RCAReport, "rca_runs"), (RepairAttempt, "repair_attempts")):
            # SUM ignores NULLs and is NULL when no run reported a value.
            results = db.execute(
                select(
                    Incident.application_id,
                    func.count(model.id),
                    func.count(model.cost_usd),
                    func.sum(model.input_tokens),
                    func.sum(model.output_tokens),
                    func.sum(model.cost_usd),
                )
                .select_from(model)
                .join(Incident, Incident.id == model.incident_id)
                .group_by(Incident.application_id)
            ).all()
            for application_id, count, with_cost, input_tokens, output_tokens, cost in results:
                row = rows.get(application_id)
                if row is None:
                    continue
                row[counter] += count
                row["runs_without_cost"] += count - with_cost
                row["input_tokens"] = _add(row["input_tokens"], input_tokens)
                row["output_tokens"] = _add(row["output_tokens"], output_tokens)
                row["cost_usd"] = _add(row["cost_usd"], float(cost) if cost is not None else None)

        items = list(rows.values())
        totals: dict[str, Any] = {key: None for key in _TOTALS}
        for item in items:
            for key in _TOTALS:
                totals[key] = _add(totals[key], item[key])
        return {"items": items, **totals}

    return router
