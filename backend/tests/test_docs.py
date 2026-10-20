"""The README documents how to run Sentinel and every Settings variable."""

import re
from pathlib import Path

from app.config import Settings

README = Path(__file__).resolve().parents[2] / "README.md"


def settings_variables() -> list[str]:
    prefix = Settings.model_config["env_prefix"]
    return [f"{prefix}{name}".upper() for name in Settings.model_fields]


def missing_settings_variables(text: str) -> list[str]:
    return [
        name for name in settings_variables() if not re.search(rf"\b{re.escape(name)}\b", text)
    ]


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def test_settings_fields_are_enumerated():
    names = settings_variables()
    assert len(names) == len(Settings.model_fields) > 0
    assert "SENTINEL_DATABASE_URL" in names
    assert "SENTINEL_ADMIN_TOKEN" in names
    assert "SENTINEL_GITHUB_PUBLISH" in names
    assert all(name.startswith("SENTINEL_") for name in names)


def test_readme_documents_every_settings_variable():
    assert missing_settings_variables(_readme()) == []


def test_missing_variable_is_detected():
    text = _readme()
    removed = re.sub(r"\bSENTINEL_WORKDIR\b", "", text)
    assert missing_settings_variables(removed) == ["SENTINEL_WORKDIR"]
    # A longer variable name does not count as documenting a shorter one.
    assert "SENTINEL_WORKDIR" in missing_settings_variables("SENTINEL_WORKDIR_EXTRA")


def test_readme_has_local_run_sections():
    text = _readme()
    for heading in (
        "## Ejecutar el backend localmente",
        "## Ejecutar el frontend localmente",
        "## Ejecutar la aplicación de demostración localmente",
        "## Recorrido: de una falla al PR en borrador",
        "## Referencia de variables de entorno",
        "## Proveedores de IA",
        "## Reglas de severidad",
        "## Modelo de seguridad",
        "## Próximos pasos",
    ):
        assert heading in text, heading


def test_readme_documents_commands_and_safe_defaults():
    text = _readme()
    for snippet in (
        "uvicorn app.main:app --port 8000",
        "python -m app.seed",
        "python -m app.worker",
        "npm run dev",
        "/api",
        "DEMO_FAULTS_ENABLED",
        "SENTINEL_URL",
        "SENTINEL_INGEST_KEY",
        "SENTINEL_GITHUB_PUBLISH=false",
        "ANTHROPIC_API_KEY",
        "openai",
        "gemini",
        "Claude Code Enterprise",
        "MCP",
        "OpenTelemetry",
    ):
        assert snippet in text, snippet
