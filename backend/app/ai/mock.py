"""Deterministic mock AI provider (the default; no network, no credentials).

Outputs are chosen by a pluggable rule table evaluated against the prompt
(task and incident ``context``). The first matching rule produces the raw
output; when no rule matches, a generic schema-valid output is synthesised
from the incident data. The same input always yields the same output.
"""

import enum
import types
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, ValidationError

from app.ai.base import AIProvider, AIResult, Prompt, StructuredOutputError, T, Usage

MOCK_MODEL = "sentinel-mock-1"


@dataclass(frozen=True)
class MockRule:
    """A deterministic response rule.

    ``matches`` decides whether the rule applies to a prompt/schema pair and
    ``respond`` returns the raw output (a dict or a model instance), which is
    always validated against the requested schema.
    """

    name: str
    matches: Callable[[Prompt, type[BaseModel]], bool]
    respond: Callable[[Prompt, type[BaseModel]], dict[str, Any] | BaseModel]


def _incident_label(prompt: Prompt) -> str:
    ctx = prompt.context
    exc_type = str(ctx.get("exception_type") or "UnknownError")
    message = str(ctx.get("message") or "").strip()
    label = f"{exc_type}: {message}" if message else exc_type
    return label[:200]


def _synthesise(annotation: Any, field_name: str, prompt: Prompt, depth: int) -> Any:
    if depth > 6:
        return None
    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin in (Union, types.UnionType):
        if type(None) in args:
            return None
        return _synthesise(args[0], field_name, prompt, depth)
    if origin is Literal:
        return args[0]
    if origin in (list, set, frozenset, tuple):
        return []
    if origin is dict:
        return {}
    if isinstance(annotation, type):
        if issubclass(annotation, BaseModel):
            return _synthesise_model(annotation, prompt, depth + 1)
        if issubclass(annotation, enum.Enum):
            return next(iter(annotation)).value
        if issubclass(annotation, bool):
            return False
        if issubclass(annotation, int):
            return 0
        if issubclass(annotation, float):
            return 0.0
        if issubclass(annotation, str):
            return f"[mock] {field_name.replace('_', ' ')} for {_incident_label(prompt)}"
    return None


def _synthesise_model(schema: type[BaseModel], prompt: Prompt, depth: int = 0) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for name, info in schema.model_fields.items():
        if not info.is_required():
            continue
        data[info.alias or name] = _synthesise(info.annotation, name, prompt, depth)
    return data


class MockProvider(AIProvider):
    """Clearly labelled deterministic provider. Usage is always "not available"."""

    name = "mock"
    is_mock = True

    def __init__(self, rules: list[MockRule] | None = None) -> None:
        self._rules: list[MockRule] = list(rules or [])

    @property
    def rules(self) -> tuple[MockRule, ...]:
        return tuple(self._rules)

    def add_rule(self, rule: MockRule) -> None:
        self._rules.append(rule)

    def generate_structured(self, prompt: Prompt, schema: type[T]) -> AIResult[T]:
        raw: dict[str, Any] | BaseModel | None = None
        for rule in self._rules:
            if rule.matches(prompt, schema):
                raw = rule.respond(prompt, schema)
                break
        if raw is None:
            raw = _synthesise_model(schema, prompt)
        if isinstance(raw, BaseModel):
            raw = raw.model_dump(mode="json")
        try:
            output = schema.model_validate(raw)
        except ValidationError as exc:
            raise StructuredOutputError(
                f"mock output does not match schema {schema.__name__}: {exc}"
            ) from exc
        return AIResult(output=output, usage=Usage(), provider=self.name, model=MOCK_MODEL)
