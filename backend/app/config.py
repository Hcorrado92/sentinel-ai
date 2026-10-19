"""Application settings loaded from SENTINEL_-prefixed environment variables."""

from pydantic_settings import BaseSettings, SettingsConfigDict

MOCK_AI_PROVIDER = "mock"


class Settings(BaseSettings):
    """Runtime configuration. Every value has a safe default.

    No ``.env`` file is read: configuration only comes from the process
    environment (or explicit constructor arguments, as used in tests).
    """

    model_config = SettingsConfigDict(
        env_prefix="SENTINEL_",
        env_file=None,
        extra="ignore",
    )

    database_url: str = "sqlite:///./sentinel.db"
    admin_token: str | None = None

    ai_provider: str = MOCK_AI_PROVIDER
    github_publish: bool = False

    # Directory where isolated repository copies are created for repairs.
    workdir: str = "./var/workdir"
    max_repair_attempts: int = 3
    job_timeout_seconds: int = 600

    # Ingestion limits.
    max_body_bytes: int = 256 * 1024
    max_message_length: int = 4096
    max_stack_trace_length: int = 64 * 1024
    max_metadata_items: int = 50
    max_metadata_bytes: int = 8 * 1024

    @property
    def ai_provider_is_mock(self) -> bool:
        return self.ai_provider.strip().lower() == MOCK_AI_PROVIDER


def get_settings() -> Settings:
    return Settings()
