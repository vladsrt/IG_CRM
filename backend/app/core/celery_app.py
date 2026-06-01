"""Celery app instance.

Start the default worker (uploads, warmup, profile edits):

    celery -A app.core.celery_app.celery_app worker \
        -Q ig_crm.default \
        --loglevel=info \
        --concurrency=2 \
        --hostname=worker-default@%h

Start the dedicated stats worker (lightweight metrics collection):

    celery -A app.core.celery_app.celery_app worker \
        -Q stats_queue \
        --loglevel=info \
        --concurrency=1 \
        --hostname=worker-stats@%h

Start the beat scheduler:

    celery -A app.core.celery_app.celery_app beat --loglevel=info

All-in-one for local development (never use in production):

    celery -A app.core.celery_app.celery_app worker \
        -Q ig_crm.default,stats_queue \
        --loglevel=info \
        --concurrency=2 \
        -B

Task modules are picked up from app.workers.celery_tasks, so
run_instagram_task.delay(...) works from anywhere as long as the worker
process imported this module.
"""

from __future__ import annotations

from celery import Celery
from celery.signals import worker_process_init

from app.core.config import settings
from app.core.logging_config import setup_logging


@worker_process_init.connect
def _init_worker_logging(**_kwargs) -> None:  # type: ignore[no-untyped-def]
    """Each forked Celery worker child re-runs logging setup so it writes to
    the same rotating file the master uses. Without this, only the parent
    process's stdout gets captured."""
    setup_logging(component="worker")

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


# task routing: stats collection runs on a dedicated queue so it never
# competes with heavy browser tasks (uploads, warmup) for worker slots.
celery_app.conf.task_routes = {
    "ig_crm.gather_account_metrics": {"queue": "stats_queue"},
}


# beat schedule
celery_app.conf.beat_schedule = {
    # janitor: sweep stuck RUNNING tasks every minute. With the 15 min
    # staleness threshold (_REAP_AFTER_SECONDS in celery_tasks.py) this means
    # a dead worker's RUNNING row frees the account within ~16 min worst-case.
    # Paired with the eager sibling reap inside run_instagram_task itself.
    "reap-stale-tasks-every-min": {
        "task": "ig_crm.reap_stale_tasks",
        "schedule": 60.0,  # seconds
        "args": (),
    },
    # stats dispatcher: fetch all active accounts and dispatch a
    # gather_account_metrics task to the stats_queue for each one.
    "dispatch-stats-collection-every-1h": {
        "task": "ig_crm.dispatch_stats_collection",
        "schedule": 3600.0,  # seconds
        "args": (),
    },
}


@celery_app.task(name="ig_crm.ping")
def ping() -> str:
    """Tiny health-check task. Use to check the worker is reachable."""
    return "pong"

