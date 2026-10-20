"""Provider-agnostic interface for structured AI generation."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class AIProviderError(Exception):
    """Base class for AI provider failures."""


class ProviderNotConfigured(AIProviderError):
    """The requested provider is unknown or lacks server-side credentials."""


class StructuredOutputError(AIProviderError):
    """The provider output is not valid JSON or does not match the schema."""


class NoPatchAvailable(AIProviderError):
    """The provider has no patch for the incident (``no_patch_available``).

    The repair engine stops with ``stop_reason`` ``unverifiable`` instead of
    retrying: without a patch there is nothing the tests could verify.
    """

    code = "no_patch_available"


@dataclass(frozen=True)
class Prompt:
    """Input for a structured generation.

    ``context`` carries the structured incident data (exception type, message,
    stack trace, files...) so the mock provider can apply deterministic rules
    without parsing free text.
    """

    user: str
    system: str = ""
    task: str = ""
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Usage:
    """Usage reported by the provider; ``None`` means "not available"."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class AIResult(Generic[T]):
    output: T
    usage: Usage
    provider: str
    model: str | None = None


class AIProvider(ABC):
    """A provider that returns Pydantic-validated structured output."""

    name: str
    is_mock: bool = False

    @abstractmethod
    def generate_structured(self, prompt: Prompt, schema: type[T]) -> AIResult[T]:
        """Generate an instance of ``schema`` for ``prompt``.

        Raises ``StructuredOutputError`` when the output cannot be validated.
        """
