from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Central config, populated from environment variables / .env file.

    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = ""

    api_key: str = ""

    # Configurable threshold used by the discrepancy detector to flag a payment that was
    # processed but never settled.
    unsettled_threshold_hours: int = 24

    # Reference "now" for reconciliation math. Defaults to real wall-clock time.
    # Overridable for reproducible demos against historical sample data.
    reconciliation_reference_time: str | None = None

    app_env: str = "development"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
