"""Demo application configuration, read from environment variables.

- ``DEMO_ENVIRONMENT``: deployment environment name (default ``development``).
- ``DEMO_FAULTS_ENABLED``: enables the intentional faults. Defaults to ``true``
  in the ``development`` environment and ``false`` elsewhere.

The faults are never allowed in ``production``: explicitly enabling them there
raises :class:`ConfigurationError` at startup.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

PRODUCTION = "production"
DEVELOPMENT = "development"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off", ""}


class ConfigurationError(RuntimeError):
    """Raised when the demo application configuration is unsafe or invalid."""


@dataclass(frozen=True)
class DemoSettings:
    environment: str = DEVELOPMENT
    faults_enabled: bool = True

    def __post_init__(self) -> None:
        if self.environment == PRODUCTION and self.faults_enabled:
            raise ConfigurationError(
                "DEMO_FAULTS_ENABLED cannot be true when DEMO_ENVIRONMENT is 'production'"
            )


def _parse_bool(name: str, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ConfigurationError(f"{name} must be a boolean, got {value!r}")


def load_settings(environ: Mapping[str, str] | None = None) -> DemoSettings:
    """Build settings from ``environ`` (defaults to ``os.environ``)."""
    env = os.environ if environ is None else environ
    environment = env.get("DEMO_ENVIRONMENT", DEVELOPMENT).strip().lower() or DEVELOPMENT
    raw_faults = env.get("DEMO_FAULTS_ENABLED")
    if raw_faults is None:
        faults_enabled = environment == DEVELOPMENT
    else:
        faults_enabled = _parse_bool("DEMO_FAULTS_ENABLED", raw_faults)
    return DemoSettings(environment=environment, faults_enabled=faults_enabled)
