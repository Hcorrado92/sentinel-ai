"""Structured RCA report produced by the AI provider and validated with Pydantic.

The model can only propose hypotheses: any ``status`` it returns is
downgraded to ``"hypothesis"`` on validation. A hypothesis becomes
``"confirmed"`` only through ``confirm_hypothesis``, which the repair engine
calls once a repair verified by tests backs it up.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

HypothesisStatus = Literal["hypothesis", "confirmed"]


class RCAHypothesis(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(max_length=5000)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(min_length=1, max_length=20)
    status: HypothesisStatus = "hypothesis"

    @field_validator("status", mode="before")
    @classmethod
    def _never_confirmed_by_model(cls, value: object) -> str:
        # Confirmation requires a test-verified repair, never the model's word.
        return "hypothesis"


class SuspectLocation(BaseModel):
    file: str = Field(min_length=1, max_length=500)
    function: str | None = Field(default=None, max_length=300)
    reason: str = Field(max_length=2000)


class RCAReportModel(BaseModel):
    summary: str = Field(min_length=1, max_length=5000)
    hypotheses: list[RCAHypothesis] = Field(max_length=10)
    suspect_locations: list[SuspectLocation] = Field(default_factory=list, max_length=20)
    uncertainties: list[str] = Field(default_factory=list, max_length=20)
    next_steps: list[str] = Field(default_factory=list, max_length=20)


def confirm_hypothesis(report: RCAReportModel, index: int) -> RCAReportModel:
    """Return a copy of ``report`` with hypothesis ``index`` marked confirmed.

    Only to be used when a repair verified by the repository tests supports it.
    """
    hypotheses = list(report.hypotheses)
    hypotheses[index] = hypotheses[index].model_copy(update={"status": "confirmed"})
    return report.model_copy(update={"hypotheses": hypotheses})
