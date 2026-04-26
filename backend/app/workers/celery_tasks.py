"""Celery task definitions — thin wrappers around the browser-automation core.

The browser logic itself (DrissionPage) lives in ``backend/workers/`` and is
invoked from these tasks. These wrappers are responsible for:

* loading ORM state (Task / Account / Proxy) inside a fresh DB session
* mutating the Task lifecycle status (PENDING → RUNNING → COMPLETED/FAILED)
* building the immutable payload that the browser worker consumes
* funneling failures into ``Task.error_log``
"""

from __future__ import annotations

import logging
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any

from celery import Task as CeleryTask
from celery.exceptions import SoftTimeLimitExceeded

from app.core.celery_app import celery_app
from app.core.database import SessionLocal
from app.models.account import InstagramAccount
from app.models.proxy import Proxy
from app.models.task import Task, TaskStatus
from workers.core.executor import TaskExecutor

logger = logging.getLogger(__name__)


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
        1. Load Task + Account + Proxy from the DB.
        2. Build the ``payload`` dict the browser worker consumes.
        3. Flip Task → RUNNING.
        4. Hand the payload to ``TaskExecutor`` (which guarantees teardown).
        5. On success → COMPLETED. On error → FAILED + write traceback.
    """
    task_uuid = uuid.UUID(task_id)
    logger.info("[run_instagram_task] task_id=%s", task_uuid)

    # ── 1. Load + 2. Build payload ─────────────────────────────────────
    with SessionLocal() as db:
        task = db.get(Task, task_uuid)
        if task is None:
            raise ValueError(f"Task {task_uuid} not found")

        account = db.get(InstagramAccount, task.account_id)
        if account is None:
            raise ValueError(
                f"InstagramAccount {task.account_id} not found for task {task_uuid}"
            )

        proxy = account.proxy  # eager-loaded via lazy="selectin" on the relationship

        # The AI parser stores the full plan dict on Task.payload:
        #   {"summary": ..., "priority": ..., "commands": [{action, args}, ...]}
        # The executor expects payload["commands"] to be the *list* — flatten here.
        plan: dict[str, Any] = task.payload or {}
        commands_list: list[dict[str, Any]] = plan.get("commands") or []

        payload: dict[str, Any] = {
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

    # ── 3. Mark RUNNING ────────────────────────────────────────────────
    _set_task_status(task_uuid, TaskStatus.RUNNING)

    # ── 4. Execute via TaskExecutor (Sprint 4) ─────────────────────────
    try:
        result: dict[str, Any] = TaskExecutor(payload).execute()

    except SoftTimeLimitExceeded as exc:
        logger.error("[run_instagram_task] soft time limit hit for %s", task_uuid)
        _set_task_status(
            task_uuid, TaskStatus.FAILED, error_log=f"SoftTimeLimitExceeded: {exc}"
        )
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
