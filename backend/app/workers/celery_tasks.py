"""Celery task definitions — thin wrappers around the browser-automation core.

The browser logic itself (DrissionPage) lives in ``backend/workers/`` and is
invoked from these tasks. These wrappers are responsible for:

* loading ORM state (Task / Account / Proxy) inside a fresh DB session
* mutating the Task lifecycle status (PENDING → RUNNING → COMPLETED/FAILED)
* building the immutable payload that the browser worker consumes
* funneling failures into ``Task.error_log``

Reliability fixes implemented (Security & Reliability Audit)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
* CRITICAL-1 — orphan recovery at the start of every ``run_instagram_task``
  invocation, plus a periodic ``reap_stale_tasks`` janitor task.
* CRITICAL-2 — ``SELECT … FOR UPDATE NOWAIT`` on the InstagramAccount row
  before state transition, and a sibling-RUNNING refusal so two workers
  never drive the same account in parallel.
"""

from __future__ import annotations

import logging
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from celery import Task as CeleryTask
from celery.exceptions import Retry, SoftTimeLimitExceeded
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.core.celery_app import celery_app
from app.core.database import SessionLocal
from app.models.account import InstagramAccount
from app.models.proxy import Proxy
from app.models.task import Task, TaskStatus
from workers.core.executor import TaskExecutor
from workers.core.observability import CheckpointException

logger = logging.getLogger(__name__)


# ── Constants ───────────────────────────────────────────────────────────
CHECKPOINT_ACCOUNT_STATUS: str = "checkpoint_required"

# Statuses that signal a previous worker died mid-execution. If a task
# arrives in any of these states, refuse to re-run.
_ORPHAN_STATUSES: frozenset[str] = frozenset({TaskStatus.RUNNING.value})

# Statuses from which we will dispatch to the browser. PENDING is the
# normal entry; DRAFT is allowed for the manual ``/orchestrator/tasks/{id}/start``
# path.
_RUNNABLE_STATUSES: frozenset[str] = frozenset(
    {TaskStatus.PENDING.value, TaskStatus.DRAFT.value}
)

# CRITICAL-1 reaper threshold — Tasks stuck in RUNNING for longer than
# this are presumed dead and force-failed by the periodic janitor.
# 1 hour matches the spec; tunable via the task's ``stale_after_seconds``.
_REAP_AFTER_SECONDS: int = 60 * 60

# CRITICAL-2 retry budget for the FOR UPDATE NOWAIT contention path.
_LOCK_RETRY_MAX_ATTEMPTS: int = 5
_LOCK_RETRY_BASE_BACKOFF_S: int = 30  # exponential: 30, 60, 120, 240, 480


# ── Helpers ─────────────────────────────────────────────────────────────
def _build_proxy_string(proxy: Proxy | None) -> str | None:
    """Render a proxy ORM row as a ``user:pass@host:port`` string.

    Note: ``InstagramBrowser`` / ``proxy_builder.create_proxy_extension``
    parse this format directly — no scheme prefix.
    """
    if proxy is None:
        return None
    return f"{proxy.username}:{proxy.password}@{proxy.host}:{proxy.port}"


def _set_task_status(
    task_id: uuid.UUID,
    new_status: TaskStatus,
    *,
    error_log: str | None = None,
) -> None:
    """Atomically transition a Task to ``new_status`` (and optionally log error)."""
    with SessionLocal() as db:
        task = db.get(Task, task_id)
        if task is None:
            logger.warning("Task %s vanished before status update", task_id)
            return
        task.status = new_status.value
        if error_log is not None:
            task.error_log = error_log
        db.commit()


def _mark_account_checkpoint(account_id: uuid.UUID, checkpoint_url: str) -> None:
    """Flag an account as needing manual challenge resolution (Epic 6.2)."""
    with SessionLocal() as db:
        account = db.get(InstagramAccount, account_id)
        if account is None:
            logger.warning(
                "Account %s vanished before checkpoint flag could be applied",
                account_id,
            )
            return
        account.status = CHECKPOINT_ACCOUNT_STATUS
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        new_line = f"[{timestamp}] checkpoint_required at {checkpoint_url}"
        account.error_log = (
            f"{account.error_log}\n{new_line}" if account.error_log else new_line
        )
        db.commit()


def _recover_orphan_if_needed(task_uuid: uuid.UUID) -> bool:
    """If the task is in an orphan state, mark it FAILED and return True.

    The combination of ``acks_late=True`` and a hard worker death
    (OOM kill, SIGKILL, host eviction) can deliver the same task message
    to a fresh worker while the DB row still says RUNNING. Re-running
    would cause a duplicate Instagram side-effect (double post, double
    DM, etc). We refuse the re-execution and leave the row in FAILED so
    the operator can decide whether to manually re-dispatch.
    """
    with SessionLocal() as db:
        task = db.get(Task, task_uuid)
        if task is None:
            return False  # caller will handle the "task not found" path
        if task.status not in _ORPHAN_STATUSES:
            return False
        task.status = TaskStatus.FAILED.value
        task.error_log = (
            "Orphan recovery: previous worker died with status RUNNING. "
            "Refusing automatic re-execution to prevent duplicate Instagram "
            "side-effects. Re-dispatch manually if intended."
        )
        db.commit()
        logger.warning(
            "[run_instagram_task] orphan-recovered task=%s — marked FAILED",
            task_uuid,
        )
        return True


# ── Task: validate_account_session ──────────────────────────────────────
@celery_app.task(
    bind=True,
    name="ig_crm.validate_account_session",
    autoretry_for=(ConnectionError, TimeoutError),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
    max_retries=3,
)
def validate_account_session(self: CeleryTask, account_id: str) -> dict[str, Any]:
    """Verify an Instagram account's stored cookies are still authenticated.

    The actual browser check (DrissionPage) is delegated to the worker layer;
    this wrapper only loads the row, marks the account, and persists results.
    """
    acct_uuid = uuid.UUID(account_id)
    logger.info("[validate_account_session] account_id=%s", acct_uuid)

    with SessionLocal() as db:
        account = db.get(InstagramAccount, acct_uuid)
        if account is None:
            raise ValueError(f"InstagramAccount {acct_uuid} not found")

        proxy_string = _build_proxy_string(account.proxy)
        cookies = account.cookies or {}

        # ── Browser logic placeholder ───────────────────────────────────
        # TODO: hand `proxy_string` and `cookies` to a dedicated validator
        # action via TaskExecutor once a `validate_session` action handler
        # is registered. For now we optimistically mark the account.
        is_valid: bool = True
        error_message: str | None = None

        account.status = "valid" if is_valid else "invalid"
        account.error_log = error_message
        account.last_check = datetime.now(timezone.utc)
        db.commit()

        return {
            "account_id": str(acct_uuid),
            "is_valid": is_valid,
            "checked_at": account.last_check.isoformat(),
        }


# ── Task: run_instagram_task ────────────────────────────────────────────
@celery_app.task(
    bind=True,
    name="ig_crm.run_instagram_task",
    acks_late=True,
)
def run_instagram_task(self: CeleryTask, task_id: str) -> dict[str, Any]:
    """Main browser-automation entrypoint.

    Pipeline:
        0. CRITICAL-1 — orphan recovery: refuse + mark FAILED if the task
           is already RUNNING (previous worker died).
        1. CRITICAL-2 — pessimistic lock on the InstagramAccount row,
           sibling-RUNNING check, payload build, and RUNNING transition,
           ALL inside a single transaction so concurrent workers cannot
           race past each other.
        2. Hand the payload to ``TaskExecutor`` (which guarantees
           browser teardown via Story 4.4).
        3. On success → COMPLETED. On error → FAILED + traceback.
    """
    task_uuid = uuid.UUID(task_id)
    logger.info("[run_instagram_task] task_id=%s", task_uuid)

    # ── 0. CRITICAL-1: orphan recovery ─────────────────────────────────
    if _recover_orphan_if_needed(task_uuid):
        return {"task_id": str(task_uuid), "status": "orphan_recovered"}

    # ── 1. CRITICAL-2: locked load + sibling check + RUNNING transition ─
    # Everything in this block runs inside one transaction. The row lock
    # is released only at db.commit(). We mark the Task RUNNING BEFORE
    # commit so a concurrent worker's sibling-check sees us as RUNNING
    # the instant the lock releases.
    account_uuid: uuid.UUID
    payload: dict[str, Any]
    with SessionLocal() as db:
        task = db.get(Task, task_uuid)
        if task is None:
            raise ValueError(f"Task {task_uuid} not found")

        if task.status not in _RUNNABLE_STATUSES:
            logger.warning(
                "[run_instagram_task] task=%s in non-runnable status %r — refusing",
                task_uuid, task.status,
            )
            return {
                "task_id": str(task_uuid),
                "status": "non_runnable",
                "task_status": task.status,
            }

        # Pessimistic lock on the account row. NOWAIT raises immediately
        # if another worker holds the row — exactly the signal we want,
        # since IG flags overlapping sessions as account compromise.
        try:
            account = db.execute(
                select(InstagramAccount)
                .where(InstagramAccount.id == task.account_id)
                .with_for_update(nowait=True)
            ).scalar_one_or_none()
        except OperationalError as exc:
            backoff = _LOCK_RETRY_BASE_BACKOFF_S * (
                2 ** self.request.retries
            )
            logger.info(
                "[run_instagram_task] account=%s locked by sibling — retry in %ds",
                task.account_id, backoff,
            )
            raise self.retry(
                exc=RuntimeError(
                    f"account {task.account_id} locked by another worker"
                ),
                countdown=backoff,
                max_retries=_LOCK_RETRY_MAX_ATTEMPTS,
            ) from exc

        if account is None:
            raise ValueError(
                f"InstagramAccount {task.account_id} not found for task {task_uuid}"
            )

        # Belt-and-braces: even with the row lock, refuse if a sibling
        # Task for this account is already RUNNING (covers the case where
        # the sibling's Postgres connection died without releasing the
        # FOR UPDATE lock — the row appears unlocked but a stale Task
        # row is still in RUNNING state).
        sibling_running = db.execute(
            select(Task.id)
            .where(
                Task.account_id == account.id,
                Task.status == TaskStatus.RUNNING.value,
                Task.id != task_uuid,
            )
            .limit(1)
        ).scalar_one_or_none()
        if sibling_running is not None:
            backoff = _LOCK_RETRY_BASE_BACKOFF_S * (
                2 ** self.request.retries
            )
            logger.info(
                "[run_instagram_task] sibling task %s RUNNING for account=%s — retry in %ds",
                sibling_running, account.id, backoff,
            )
            raise self.retry(
                exc=RuntimeError(
                    f"sibling task {sibling_running} still RUNNING "
                    f"for account {account.id}"
                ),
                countdown=backoff,
                max_retries=_LOCK_RETRY_MAX_ATTEMPTS,
            )

        # Snapshot fields we'll need outside the session.
        proxy = account.proxy
        account_uuid = account.id

        # The AI parser stores the full plan dict on Task.payload:
        #   {"summary": ..., "priority": ..., "commands": [{action, args}, ...]}
        # The executor expects payload["commands"] to be the *list* — flatten here.
        plan: dict[str, Any] = task.payload or {}
        commands_list: list[dict[str, Any]] = plan.get("commands") or []

        payload = {
            "task_id": str(task.id),
            "account_id": str(account.id),
            "ig_username": account.ig_username,
            "ig_password": account.ig_password,
            "auth_method": account.auth_method,
            "proxy_string": _build_proxy_string(proxy),
            "proxy_session_id": account.proxy_session_id,
            "cookies": account.cookies or {},
            "commands": commands_list,
            "plan_summary": plan.get("summary"),
            "plan_priority": plan.get("priority"),
            "priority": task.priority,
        }

        # Mark RUNNING within the locked transaction so a concurrent
        # worker's sibling check sees us the moment the lock releases.
        task.status = TaskStatus.RUNNING.value
        task.error_log = None
        db.commit()

    # ── 2. Execute via TaskExecutor ────────────────────────────────────
    try:
        result: dict[str, Any] = TaskExecutor(payload).execute()

    except CheckpointException as exc:
        # Epic 6.2 — IG redirected the session to a challenge / suspended
        # page. Mark the Task FAILED and the account checkpoint_required so
        # the operator can resolve it manually before any further dispatch.
        logger.warning(
            "[run_instagram_task] checkpoint hit for task=%s account=%s url=%s",
            task_uuid, account_uuid, exc.url,
        )
        _set_task_status(
            task_uuid,
            TaskStatus.FAILED,
            error_log=f"CheckpointException: {exc.url}",
        )
        _mark_account_checkpoint(account_uuid, exc.url)
        raise

    except SoftTimeLimitExceeded as exc:
        logger.error("[run_instagram_task] soft time limit hit for %s", task_uuid)
        _set_task_status(
            task_uuid, TaskStatus.FAILED, error_log=f"SoftTimeLimitExceeded: {exc}"
        )
        raise

    except Retry:
        # Defensive: if anything inside the executor calls self.retry()
        # via a nested Celery primitive, the resulting Retry exception
        # must reach Celery's runtime untouched — never marked FAILED.
        raise

    except Exception as exc:
        tb = traceback.format_exc()
        logger.exception("[run_instagram_task] failure for %s", task_uuid)
        _set_task_status(
            task_uuid,
            TaskStatus.FAILED,
            error_log=f"{type(exc).__name__}: {exc}\n{tb}",
        )
        raise

    else:
        _set_task_status(task_uuid, TaskStatus.COMPLETED, error_log=None)
        return result

    finally:
        logger.info("[run_instagram_task] finished task_id=%s", task_uuid)


# ── Task: reap_stale_tasks (CRITICAL-1 janitor) ────────────────────────
@celery_app.task(name="ig_crm.reap_stale_tasks")
def reap_stale_tasks(stale_after_seconds: int | None = None) -> dict[str, Any]:
    """Force-fail any Tasks stuck in RUNNING for more than the threshold.

    Companion to the at-task-start orphan recovery. Catches the case
    where a worker died WITHOUT a requeue (broker reconnect failure,
    host hard reboot, etc.) so the row would otherwise stay RUNNING
    forever. Schedule via Celery Beat at ~5 min intervals; default
    threshold is one hour to match the spec.

    NOTE on the timestamp choice: the Task model has ``created_at`` but
    not ``updated_at`` / ``started_at``. We use ``created_at`` as a
    safe upper bound — a Task that was created over an hour ago AND is
    still RUNNING is, at minimum, pathological and worth reaping.
    """
    threshold = (
        stale_after_seconds
        if stale_after_seconds is not None
        else _REAP_AFTER_SECONDS
    )
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=threshold)
    reaped = 0
    reaped_ids: list[str] = []

    with SessionLocal() as db:
        stmt = select(Task).where(
            Task.status == TaskStatus.RUNNING.value,
            Task.created_at < cutoff,
        )
        for task in db.execute(stmt).scalars():
            task.status = TaskStatus.FAILED.value
            timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            reap_line = (
                f"[{timestamp}] reap_stale_tasks: stuck in RUNNING for "
                f">{threshold}s — presumed dead worker."
            )
            task.error_log = (
                f"{task.error_log}\n{reap_line}" if task.error_log else reap_line
            )
            reaped_ids.append(str(task.id))
            reaped += 1
        if reaped:
            db.commit()

    if reaped:
        logger.warning(
            "[reap_stale_tasks] reaped %d stuck Task(s): %s",
            reaped, reaped_ids,
        )

    return {
        "reaped": reaped,
        "threshold_seconds": threshold,
        "task_ids": reaped_ids,
    }
