"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central application settings.

    Values are loaded from the `.env` file located in `backend/`
    and can be overridden by real environment variables at runtime.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Database ────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql+psycopg://user:password@localhost:5432/ig_crm"


# Singleton – import `settings` everywhere instead of re-instantiating.
settings = Settings()
