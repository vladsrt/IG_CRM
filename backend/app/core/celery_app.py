"""Celery app instance.

Start a worker:

    celery -A app.core.celery_app.celery_app worker --loglevel=info

Start the beat scheduler:

    celery -A app.core.celery_app.celery_app beat --loglevel=info

Task modules are picked up from app.workers.celery_tasks, so
run_instagram_task.delay(...) works from anywhere as long as the worker
process imported this module.
"""

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
    # reliability
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    # limits
    task_time_limit=settings.CELERY_TASK_TIME_LIMIT,
    task_soft_time_limit=settings.CELERY_TASK_SOFT_TIME_LIMIT,
    # result backend
    result_expires=60 * 60 * 24,  # keep results for 24h
)


# beat schedule
# janitor: sweep stuck RUNNING tasks every 5 minutes. together with the
# at-start recovery inside run_instagram_task, this stops any task from
# being stuck in RUNNING after a worker dies.
celery_app.conf.beat_schedule = {
    "reap-stale-tasks-every-5-min": {
        "task": "ig_crm.reap_stale_tasks",
        "schedule": 300.0,  # seconds
        "args": (),
    },
}


@celery_app.task(name="ig_crm.ping")
def ping() -> str:
    """Tiny health-check task. Use to check the worker is reachable."""
    return "pong"
