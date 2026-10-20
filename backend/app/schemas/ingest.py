"""Schemas for the ingestion API.

Size limits are configurable, so they are not hard-coded in the field
definitions: pass an ``IngestLimits`` as validation context
(``IngestEvent.model_validate_json(body, context={"limits": limits})``).
Without a context the defaults below apply.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    ValidationInfo,
    field_validator,
)

from app.config import Settings

MetadataKey = Annotated[str, StringConstraints(min_length=1, max_length=200)]
MetadataValue = StrictStr | StrictBool | StrictInt | StrictFloat


@dataclass(frozen=True)
class IngestLimits:
    max_message_length: int = 4096
    max_stack_trace_length: int = 64 * 1024
    max_metadata_items: int = 50
    max_metadata_bytes: int = 8 * 1024

    @classmethod
    def from_settings(cls, settings: Settings) -> "IngestLimits":
        return cls(
            max_message_length=settings.max_message_length,
            max_stack_trace_length=settings.max_stack_trace_length,
            max_metadata_items=settings.max_metadata_items,
            max_metadata_bytes=settings.max_metadata_bytes,
        )


def _limits(info: ValidationInfo) -> IngestLimits:
    context = info.context or {}
    limits = context.get("limits") if isinstance(context, dict) else None
    return limits if isinstance(limits, IngestLimits) else IngestLimits()


class IngestEvent(BaseModel):
    """An error event reported by a monitored application."""

    model_config = ConfigDict(extra="forbid")

    application: str = Field(min_length=1, max_length=200, description="Application slug")
    event_id: str | None = Field(
        default=None, min_length=1, max_length=200, description="Client-side dedup id"
    )
    timestamp: datetime
    exception_type: str = Field(min_length=1, max_length=300)
    message: str = ""
    stack_trace: str = ""
    environment: str = Field(default="production", min_length=1, max_length=100)
    correlation_id: str | None = Field(default=None, max_length=200)
    metadata: dict[MetadataKey, MetadataValue] = Field(default_factory=dict)

    @field_validator("timestamp")
    @classmethod
    def _as_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @field_validator("message")
    @classmethod
    def _message_limit(cls, value: str, info: ValidationInfo) -> str:
        limit = _limits(info).max_message_length
        if len(value) > limit:
            raise ValueError(f"message is longer than {limit} characters")
        return value

    @field_validator("stack_trace")
    @classmethod
    def _stack_trace_limit(cls, value: str, info: ValidationInfo) -> str:
        limit = _limits(info).max_stack_trace_length
        if len(value) > limit:
            raise ValueError(f"stack_trace is longer than {limit} characters")
        return value

    @field_validator("metadata")
    @classmethod
    def _metadata_limits(cls, value: dict[str, Any], info: ValidationInfo) -> dict[str, Any]:
        limits = _limits(info)
        if len(value) > limits.max_metadata_items:
            raise ValueError(f"metadata has more than {limits.max_metadata_items} keys")
        size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        if size > limits.max_metadata_bytes:
            raise ValueError(f"metadata is larger than {limits.max_metadata_bytes} bytes")
        return value


class IngestAccepted(BaseModel):
    event_id: str
    incident_id: int
    deduplicated: bool
