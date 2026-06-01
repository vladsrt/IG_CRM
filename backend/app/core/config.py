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

    # llm (OpenAI-compatible: OpenAI, Ollama Cloud, z.ai/GLM, etc.)
    OPENAI_API_KEY: str = ""
    # Base URL of the OpenAI-compatible API. Empty = OpenAI default.
    #   Ollama Cloud: https://ollama.com/v1
    #   z.ai / GLM:   https://api.z.ai/api/paas/v4
    OPENAI_BASE_URL: str = ""
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

    # jwt auth
    SECRET_KEY: str = ""  # set in .env, required for production
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    JWT_ALGORITHM: str = "HS256"

    # cors
    CORS_ALLOWED_ORIGINS: list[str] = ["*"]

    # allow running a browser task WITHOUT a proxy (direct connection from the
    # host IP). Off by default — running real IG with no proxy burns accounts.
    # When True, OR when the account owner is an admin, no-proxy runs are allowed
    # (handy for testing the pipeline without residential proxies).
    ALLOW_NO_PROXY: bool = False

    # trust gate: minimum 0-100 score for fan-out to dispatch to an account.
    # Set to 0 for testing without good proxies (lets no-proxy/weak-proxy runs
    # through). Raise back to 50 for production.
    MIN_TRUST_SCORE: int = 50

    # capacity guard: protect the test box from being overloaded by browsers.
    CAPACITY_CPU_PERCENT: float = 85.0      # don't start a task above this CPU%
    CAPACITY_RAM_PERCENT: float = 85.0      # don't start a task above this RAM%
    CAPACITY_MAX_BROWSERS: int = 4          # global ceiling of our Chromium instances
    # default parallel agent slots per tier (admin can override per-subscription)
    AGENTS_FREE: int = 1
    AGENTS_PRO: int = 5
    AGENTS_ENTERPRISE: int = 10

    # admin access: comma-separated emails that get the admin role (stats panel,
    # grant tiers/agents). Plain string to avoid pydantic-settings JSON parsing.
    # e.g. ADMIN_EMAILS=me@x.com,you@y.com
    ADMIN_EMAILS: str = ""

    # logging (consumed by app/core/logging_config.py — see that module).
    # LOG_DIR="" disables file output (dev default; stdout only).
    # Set LOG_DIR=/var/log/ig_crm in prod to capture rotated .log files.
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = ""
    LOG_MAX_BYTES: int = 20 * 1024 * 1024
    LOG_BACKUP_COUNT: int = 5

    def is_admin(self, email: str | None) -> bool:
        """True if the email is in the admin allow-list (case-insensitive)."""
        if not email:
            return False
        allow = {e.strip().lower() for e in self.ADMIN_EMAILS.split(",") if e.strip()}
        return email.lower() in allow


# singleton, import `settings` instead of building a new one
settings = Settings()
