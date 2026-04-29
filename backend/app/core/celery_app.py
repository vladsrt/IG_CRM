"""Celery application instance.

Run a worker with::

    celery -A app.core.celery_app.celery_app worker --loglevel=info

The task module is auto-discovered from ``app.workers.celery_tasks`` so that
``run_instagram_task.delay(...)`` works from anywhere in the codebase as long
as the worker process has imported this module.
"""
#celery

from __future__ import annotations

from celery import Celery

from app.core.config import settings

celery_app: Celery = Celery(
    "ig_crm",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.workers.celery_tasks", "app.workers.media_tasks"],
)

celery_app.conf.update(
    task_default_queue=settings.CELERY_TASK_DEFAULT_QUEUE,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Reliability ────────────────────────────────────────────────────────
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    # Limits ─────────────────────────────────────────────────────────────
    task_time_limit=settings.CELERY_TASK_TIME_LIMIT,
    task_soft_time_limit=settings.CELERY_TASK_SOFT_TIME_LIMIT,
    # Result backend ─────────────────────────────────────────────────────
    result_expires=60 * 60 * 24,  # keep results for 24h
)


@celery_app.task(name="ig_crm.ping")
def ping() -> str:
    """Lightweight health-check task — useful to verify worker connectivity."""
    return "pong"
