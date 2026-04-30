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
    DATABASE_URL: str = "postgresql+psycopg://ig_user:ig_password123@localhost:5433/ig_crm"

    # ── Redis / Celery ──────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"
    CELERY_TASK_DEFAULT_QUEUE: str = "ig_crm.default"
    CELERY_TASK_TIME_LIMIT: int = 60 * 30        # hard kill after 30 min
    CELERY_TASK_SOFT_TIME_LIMIT: int = 60 * 25   # raise SoftTimeLimitExceeded at 25 min

    # ── OpenAI ──────────────────────────────────────────────────────────
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_TIMEOUT_SECONDS: float = 60.0
    OPENAI_MAX_RETRIES: int = 2

    # ── Media storage / FFmpeg ──────────────────────────────────────────
    MEDIA_ROOT: str = "./media"                 # base dir for uploaded + processed assets
    MEDIA_MAX_UPLOAD_BYTES: int = 500 * 1024 * 1024   # 500 MB hard cap per upload
    FFMPEG_BIN: str = "ffmpeg"
    FFPROBE_BIN: str = "ffprobe"
    FFMPEG_TIMEOUT_SECONDS: int = 60 * 15       # 15 min per uniqueization pass
    FFMPEG_DEFAULT_BITRATE_BPS: int = 3_000_000  # fallback if ffprobe can't read input


# Singleton – import `settings` everywhere instead of re-instantiating.
settings = Settings()
