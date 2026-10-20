"""Anthropic adapter.

The adapter never builds its own client: it receives an object exposing the
``messages.create(...)`` call of the anthropic SDK. The registry builds the
real SDK client only when ``ANTHROPIC_API_KEY`` is set; tests inject a fake.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from app.ai.base import AIProvider, AIResult, Prompt, StructuredOutputError, T, Usage


@dataclass(frozen=True)
class ModelPrice:
    """Estimated price in USD per million tokens."""

    input_usd_per_mtok: float
    output_usd_per_mtok: float


DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"

_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def _response_text(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", "text") == "text":
            text = getattr(block, "text", None)
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts).strip()


def parse_json_output(text: str, schema: type[T]) -> T:
    """Parse ``text`` as JSON (optionally fenced) and validate it as ``schema``."""
    fenced = _FENCE.match(text.strip())
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise StructuredOutputError(f"provider output is not valid JSON: {exc}") from exc
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise StructuredOutputError(
            f"provider output does not match schema {schema.__name__}: {exc}"
        ) from exc


class AnthropicProvider(AIProvider):
    name = "anthropic"
    is_mock = False

    def __init__(
        self,
        client: Any,
        *,
        model: str = DEFAULT_ANTHROPIC_MODEL,
        max_tokens: int = 4096,
        prices: dict[str, ModelPrice] | None = None,
    ) -> None:
        self._client = client
        self.model = model
        self.max_tokens = max_tokens
        self.prices = dict(prices or {})

    def _instructions(self, schema: type[BaseModel]) -> str:
        return (
            "Respond with a single JSON object and nothing else (no prose, no "
            "markdown). It must validate against this JSON schema:\n"
            + json.dumps(schema.model_json_schema(), sort_keys=True)
        )

    def _cost(self, input_tokens: int | None, output_tokens: int | None) -> float | None:
        price = self.prices.get(self.model)
        if price is None or input_tokens is None or output_tokens is None:
            return None
        return round(
            (input_tokens * price.input_usd_per_mtok + output_tokens * price.output_usd_per_mtok)
            / 1_000_000,
            6,
        )

    def generate_structured(self, prompt: Prompt, schema: type[T]) -> AIResult[T]:
        system = "\n\n".join(part for part in (prompt.system, self._instructions(schema)) if part)
        response = self._client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt.user}],
        )
        output = parse_json_output(_response_text(response), schema)

        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
        return AIResult(
            output=output,
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=self._cost(input_tokens, output_tokens),
            ),
            provider=self.name,
            model=self.model,
        )
