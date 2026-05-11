"""App config, loaded from env vars."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Main app settings.

    Values come from the .env file in backend/, real env vars override them
    at runtime.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # database
    DATABASE_URL: str = "postgresql+psycopg://ig_user:ig_password123@localhost:5433/ig_crm"

    # redis and celery
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"
    CELERY_TASK_DEFAULT_QUEUE: str = "ig_crm.default"
    CELERY_TASK_TIME_LIMIT: int = 60 * 30        # hard kill after 30 min
    CELERY_TASK_SOFT_TIME_LIMIT: int = 60 * 25   # SoftTimeLimitExceeded after 25 min

    # openai
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_TIMEOUT_SECONDS: float = 60.0
    OPENAI_MAX_RETRIES: int = 2

    # media storage and ffmpeg
    MEDIA_ROOT: str = "./media"                 # base dir for uploaded and processed assets
    MEDIA_MAX_UPLOAD_BYTES: int = 500 * 1024 * 1024   # 500 MB per upload, hard cap
    FFMPEG_BIN: str = "ffmpeg"
    FFPROBE_BIN: str = "ffprobe"
    FFMPEG_TIMEOUT_SECONDS: int = 60 * 15       # 15 min per uniqueize pass
    FFMPEG_DEFAULT_BITRATE_BPS: int = 3_000_000  # used if ffprobe can not read the input


# singleton, import `settings` instead of building a new one
settings = Settings()
