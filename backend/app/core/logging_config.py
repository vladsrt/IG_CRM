"""Centralised logging setup for the API process and the Celery worker.

Goals:
- Always log to stdout (so docker/system journal capture everything).
- Optionally ALSO write rotated files under LOG_DIR for offline triage.
- One consistent format across api/worker/beat so grep is sane.

Called once per process at startup (api.main: app startup event; celery_app:
worker_ready signal).
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

# Honoured env vars (read directly, not via Settings — this module loads
# BEFORE pydantic-settings does in some boot paths).
_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
_LOG_DIR = os.getenv("LOG_DIR", "")  # empty = stdout only
_LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", str(20 * 1024 * 1024)))  # 20 MB
_LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "5"))             # keep 5 rotations

_FORMAT = (
    "%(asctime)s [%(levelname)s] %(name)s "
    "(%(filename)s:%(lineno)d): %(message)s"
)


def setup_logging(component: str = "app") -> None:
    """Configure the root logger.

    Args:
        component: short tag used as the log file name (e.g. "api",
            "worker", "beat"). Determines the file when LOG_DIR is set.
    """
    root = logging.getLogger()
    # remove anything inherited from uvicorn/celery default config so we don't
    # double-emit every line.
    for h in list(root.handlers):
        root.removeHandler(h)

    root.setLevel(_LOG_LEVEL)
    fmt = logging.Formatter(_FORMAT)

    # stdout — always on. Docker captures this, dev terminal sees it directly.
    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(fmt)
    root.addHandler(stdout)

    # rotating file — only when LOG_DIR is configured (prod). Default OFF in
    # dev so we don't pollute the repo with .log files.
    if _LOG_DIR:
        try:
            os.makedirs(_LOG_DIR, exist_ok=True)
            log_path = os.path.join(_LOG_DIR, f"{component}.log")
            file_handler = RotatingFileHandler(
                log_path,
                maxBytes=_LOG_MAX_BYTES,
                backupCount=_LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
            logging.getLogger(__name__).info(
                "logging: writing to %s (rotate %d MB x %d)",
                log_path, _LOG_MAX_BYTES // (1024 * 1024), _LOG_BACKUP_COUNT,
            )
        except Exception as exc:  # never let logging setup kill the app
            logging.getLogger(__name__).warning(
                "logging: failed to enable file handler at %s: %s",
                _LOG_DIR, exc,
            )

    # Calm down the noisy libraries. They still log WARNINGS+.
    for noisy in ("urllib3", "asyncio", "watchfiles", "DrissionPage"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
