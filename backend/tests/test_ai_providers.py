import json
import sys
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest
from pydantic import BaseModel, Field

from app.ai.anthropic_provider import AnthropicProvider, ModelPrice
from app.ai.base import AIProvider, Prompt, ProviderNotConfigured, StructuredOutputError
from app.ai.mock import MockProvider, MockRule
from app.ai.registry import get_provider, provider_statuses
from app.config import Settings


@pytest.fixture(autouse=True)
def _no_real_anthropic(monkeypatch):
    """Any attempt to import the real SDK during these tests fails loudly."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "anthropic", None)


class Confidence(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


class Hypothesis(BaseModel):
    description: str
    confidence: Confidence
    evidence: list[str] = Field(default_factory=list)


class Report(BaseModel):
    summary: str
    status: Literal["hypothesis", "confirmed"]
    hypotheses: list[Hypothesis]
    top: Hypothesis
    score: float
    attempts: int
    notes: str | None


def _prompt(**context) -> Prompt:
    return Prompt(
        task="rca",
        system="You are an SRE.",
        user="Analyse the incident.",
        context={"exception_type": "KeyError", "message": "'price'", **context},
    )


# --- registry -------------------------------------------------------------


def test_default_settings_return_mock_provider():
    provider = get_provider(Settings(), env={})

    assert isinstance(provider, AIProvider)
    assert provider.name == "mock"
    assert provider.is_mock is True


def test_anthropic_without_key_raises_not_configured():
    with pytest.raises(ProviderNotConfigured, match="not configured"):
        get_provider(Settings(ai_provider="anthropic"), env={})


def test_anthropic_without_key_in_process_env_raises_not_configured():
    with pytest.raises(ProviderNotConfigured):
        get_provider(Settings(ai_provider="anthropic"))


@pytest.mark.parametrize("name", ["openai", "gemini", "OpenAI"])
def test_extension_providers_always_not_configured(name):
    env = {"ANTHROPIC_API_KEY": "fake", "OPENAI_API_KEY": "fake", "GEMINI_API_KEY": "fake"}
    with pytest.raises(ProviderNotConfigured, match="not configured"):
        get_provider(Settings(ai_provider=name), env=env)


def test_unknown_provider_not_configured():
    with pytest.raises(ProviderNotConfigured, match="not configured"):
        get_provider(Settings(ai_provider="nope"), env={})


def test_anthropic_with_key_uses_injected_client_factory():
    seen = []
    fake_client = object()

    def factory(api_key):
        seen.append(api_key)
        return fake_client

    settings = Settings(
        ai_provider="anthropic",
        anthropic_model="test-model",
        anthropic_input_usd_per_mtok=1.0,
        anthropic_output_usd_per_mtok=2.0,
    )
    provider = get_provider(
        settings, env={"ANTHROPIC_API_KEY": "fictitious"}, anthropic_client_factory=factory
    )

    assert seen == ["fictitious"]
    assert provider.name == "anthropic"
    assert provider.is_mock is False
    assert provider.model == "test-model"
    assert provider.prices == {"test-model": ModelPrice(1.0, 2.0)}


def test_provider_statuses():
    statuses = {s["name"]: s for s in provider_statuses(Settings(), env={})}

    assert list(statuses) == ["mock", "anthropic", "openai", "gemini"]
    assert statuses["mock"]["selected"] is True
    assert statuses["mock"]["configured"] is True
    assert statuses["mock"]["is_mock"] is True
    assert statuses["anthropic"]["configured"] is False
    assert statuses["openai"]["configured"] is False
    assert statuses["gemini"]["configured"] is False

    with_key = {
        s["name"]: s
        for s in provider_statuses(Settings(ai_provider="anthropic"), env={"ANTHROPIC_API_KEY": "x"})
    }
    assert with_key["anthropic"]["configured"] is True
    assert with_key["anthropic"]["selected"] is True
    assert with_key["mock"]["selected"] is False


def test_health_lists_provider_statuses(client):
    body = client.get("/api/v1/health").json()

    providers = {p["name"]: p for p in body["providers"]}
    assert providers["mock"] == {
        "name": "mock",
        "selected": True,
        "configured": True,
        "is_mock": True,
        "detail": providers["mock"]["detail"],
    }
    assert providers["anthropic"]["configured"] is False
    assert providers["openai"]["configured"] is False
    assert providers["gemini"]["configured"] is False


# --- mock provider ----------------------------------------------------------


def test_mock_is_deterministic_and_cost_not_available():
    provider = MockProvider()

    first = provider.generate_structured(_prompt(), Report)
    second = provider.generate_structured(_prompt(), Report)

    assert isinstance(first.output, Report)
    assert first.output == second.output
    assert first.output.model_dump() == second.output.model_dump()
    assert first.usage.cost_usd is None
    assert first.usage.input_tokens is None
    assert first.usage.output_tokens is None
    assert first.provider == "mock"


def test_mock_generic_output_reflects_incident_data():
    output = MockProvider().generate_structured(_prompt(), Report).output

    assert "KeyError" in output.summary
    assert output.summary.startswith("[mock]")
    assert output.status == "hypothesis"
    assert output.top.confidence is Confidence.low
    assert output.notes is None

    other = MockProvider().generate_structured(
        _prompt(exception_type="ZeroDivisionError"), Report
    ).output
    assert "ZeroDivisionError" in other.summary


def test_mock_rule_table_takes_precedence():
    rule = MockRule(
        name="zero-division",
        matches=lambda prompt, schema: prompt.context.get("exception_type") == "ZeroDivisionError",
        respond=lambda prompt, schema: {
            "summary": "Division by zero in discount calculation",
            "status": "hypothesis",
            "hypotheses": [],
            "top": {"description": "quantity may be zero", "confidence": "medium"},
            "score": 0.5,
            "attempts": 1,
            "notes": None,
        },
    )
    provider = MockProvider(rules=[rule])

    matched = provider.generate_structured(_prompt(exception_type="ZeroDivisionError"), Report)
    unmatched = provider.generate_structured(_prompt(), Report)

    assert matched.output.summary == "Division by zero in discount calculation"
    assert matched.output.top.confidence is Confidence.medium
    assert unmatched.output.summary.startswith("[mock]")
    assert [r.name for r in provider.rules] == ["zero-division"]


def test_mock_rule_with_invalid_output_raises_structured_error():
    provider = MockProvider()
    provider.add_rule(MockRule("bad", lambda p, s: True, lambda p, s: {"summary": 1}))

    with pytest.raises(StructuredOutputError):
        provider.generate_structured(_prompt(), Report)


# --- anthropic adapter ------------------------------------------------------


class FakeMessages:
    def __init__(self, text, input_tokens=1200, output_tokens=300):
        self.text = text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)],
            usage=SimpleNamespace(
                input_tokens=self.input_tokens, output_tokens=self.output_tokens
            ),
        )


class FakeClient:
    def __init__(self, text, **kwargs):
        self.messages = FakeMessages(text, **kwargs)


class Small(BaseModel):
    summary: str
    confidence: float


def _adapter(text, **kwargs):
    client = FakeClient(text, **kwargs)
    provider = AnthropicProvider(
        client,
        model="fake-model",
        max_tokens=512,
        prices={"fake-model": ModelPrice(input_usd_per_mtok=3.0, output_usd_per_mtok=15.0)},
    )
    return provider, client


def test_anthropic_adapter_valid_json_produces_model_and_usage():
    provider, client = _adapter(json.dumps({"summary": "null price", "confidence": 0.4}))

    result = provider.generate_structured(_prompt(), Small)

    assert result.output == Small(summary="null price", confidence=0.4)
    assert result.usage.input_tokens == 1200
    assert result.usage.output_tokens == 300
    assert result.usage.cost_usd == pytest.approx((1200 * 3.0 + 300 * 15.0) / 1_000_000)
    assert result.provider == "anthropic"
    assert result.model == "fake-model"

    call = client.messages.calls[0]
    assert call["model"] == "fake-model"
    assert call["max_tokens"] == 512
    assert call["messages"] == [{"role": "user", "content": "Analyse the incident."}]
    assert "JSON" in call["system"]
    assert "You are an SRE." in call["system"]


def test_anthropic_adapter_accepts_fenced_json():
    provider, _ = _adapter('```json\n{"summary": "s", "confidence": 1}\n```')

    assert provider.generate_structured(_prompt(), Small).output.summary == "s"


def test_anthropic_adapter_unknown_model_price_gives_none_cost():
    client = FakeClient(json.dumps({"summary": "s", "confidence": 0.1}))
    provider = AnthropicProvider(client, model="unpriced")

    result = provider.generate_structured(_prompt(), Small)

    assert result.usage.input_tokens == 1200
    assert result.usage.cost_usd is None


def test_anthropic_adapter_invalid_json_raises():
    provider, _ = _adapter("I think the root cause is a null value.")

    with pytest.raises(StructuredOutputError, match="not valid JSON"):
        provider.generate_structured(_prompt(), Small)


def test_anthropic_adapter_schema_mismatch_raises():
    provider, _ = _adapter(json.dumps({"summary": "s", "confidence": "very"}))

    with pytest.raises(StructuredOutputError, match="does not match schema"):
        provider.generate_structured(_prompt(), Small)


# --- hermeticity ------------------------------------------------------------


def test_ai_modules_do_not_import_anthropic_sdk_at_module_level():
    ai_dir = Path(__file__).resolve().parents[1] / "app" / "ai"
    for path in ai_dir.glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(("import anthropic", "from anthropic")):
                pytest.fail(f"{path.name} imports the anthropic SDK at module level")


def test_tests_never_import_real_sdk():
    assert sys.modules.get("anthropic") is None
    with pytest.raises(ImportError):
        import anthropic  # noqa: F401
