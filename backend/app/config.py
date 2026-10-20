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
    # Anthropic adapter (used only when ai_provider == "anthropic" and the
    # ANTHROPIC_API_KEY environment variable is set on the server).
    anthropic_model: str = "claude-sonnet-5-5"
    anthropic_max_tokens: int = 4096
    # Estimated prices in USD per million tokens for ``anthropic_model``.
    anthropic_input_usd_per_mtok: float = 3.0
    anthropic_output_usd_per_mtok: float = 15.0

    github_publish: bool = False

    # Directory where isolated repository copies are created for repairs.
    workdir: str = "./var/workdir"
    max_repair_attempts: int = 3
    job_timeout_seconds: int = 600
    # Limits for patches proposed by the repair engine.
    max_patch_files: int = 20
    max_patch_bytes: int = 200 * 1024

    # Job queue / worker.
    # When true, API endpoints that enqueue a job process it before responding.
    run_jobs_inline: bool = False
    job_max_attempts: int = 3
    job_retry_backoff_seconds: int = 30
    worker_poll_seconds: float = 2.0

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
