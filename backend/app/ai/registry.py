"""Provider selection from settings and the server environment.

- ``mock`` (default): deterministic, always available.
- ``anthropic``: only when ``ANTHROPIC_API_KEY`` is set in the environment.
- ``openai`` / ``gemini``: documented extension points; not implemented yet,
  so they always raise ``ProviderNotConfigured``. To add one, implement an
  ``AIProvider`` adapter and build it here when its credentials exist.
"""

import os
from collections.abc import Callable, Mapping
from typing import Any

from app.ai.anthropic_provider import AnthropicProvider, ModelPrice
from app.ai.base import AIProvider, ProviderNotConfigured
from app.ai.mock import MockProvider
from app.config import Settings

ANTHROPIC_KEY_ENV = "ANTHROPIC_API_KEY"
EXTENSION_PROVIDERS = ("openai", "gemini")
KNOWN_PROVIDERS = ("mock", "anthropic", *EXTENSION_PROVIDERS)

ClientFactory = Callable[[str], Any]


def _anthropic_sdk_client(api_key: str) -> Any:
    import anthropic

    return anthropic.Anthropic(api_key=api_key)


def get_provider(
    settings: Settings,
    env: Mapping[str, str] | None = None,
    *,
    anthropic_client_factory: ClientFactory | None = None,
) -> AIProvider:
    env = os.environ if env is None else env
    name = settings.ai_provider.strip().lower()

    if name == "mock":
        return MockProvider()
    if name == "anthropic":
        api_key = env.get(ANTHROPIC_KEY_ENV)
        if not api_key:
            raise ProviderNotConfigured(
                f"anthropic provider not configured: {ANTHROPIC_KEY_ENV} is not set"
            )
        factory = anthropic_client_factory or _anthropic_sdk_client
        return AnthropicProvider(
            factory(api_key),
            model=settings.anthropic_model,
            max_tokens=settings.anthropic_max_tokens,
            prices={
                settings.anthropic_model: ModelPrice(
                    input_usd_per_mtok=settings.anthropic_input_usd_per_mtok,
                    output_usd_per_mtok=settings.anthropic_output_usd_per_mtok,
                )
            },
        )
    if name in EXTENSION_PROVIDERS:
        raise ProviderNotConfigured(f"{name} provider not configured")
    raise ProviderNotConfigured(f"unknown AI provider '{name}': not configured")


def provider_statuses(
    settings: Settings, env: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """Availability of every known provider, without building any client."""
    env = os.environ if env is None else env
    selected = settings.ai_provider.strip().lower()
    has_key = bool(env.get(ANTHROPIC_KEY_ENV))
    configured = {"mock": True, "anthropic": has_key}
    details = {
        "mock": "deterministic mock; costs not available",
        "anthropic": "configured" if has_key else f"not configured: {ANTHROPIC_KEY_ENV} is not set",
    }
    return [
        {
            "name": name,
            "selected": name == selected,
            "configured": configured.get(name, False),
            "is_mock": name == "mock",
            "detail": details.get(name, "not configured"),
        }
        for name in KNOWN_PROVIDERS
    ]
