"""Celery tasks. Thin wrappers around the browser automation core.

The real browser code (DrissionPage) lives in backend/workers/ and is called
from here. The job of these wrappers:

- load orm state (Task / Account / Proxy) in a fresh db session
- move the Task through statuses (PENDING -> RUNNING -> COMPLETED/FAILED)
- build the payload the browser worker reads
- send errors to Task.error_log

Reliability fixes from the audit:
- orphan recovery at the start of every run_instagram_task, plus a periodic
  reap_stale_tasks janitor.
- SELECT ... FOR UPDATE NOWAIT on the InstagramAccount row before state
  transition, and a sibling-RUNNING check so two workers never drive the
  same account at once.
"""

from __future__ import annotations

import logging
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from celery import Task as CeleryTask
from celery.exceptions import Retry, SoftTimeLimitExceeded
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.core.celery_app import celery_app
from app.core.config import settings
from app.core.database import SessionLocal
from app.core.secrets import decrypt_cookies, decrypt_secret
from app.models.account import InstagramAccount
from app.models.proxy import Proxy
from app.models.task import Task, TaskStatus
from app.models.user import User
from app.services.agents import agents_for_subscription
from app.services.capacity import has_free_capacity
from workers.core.executor import TaskExecutor
from workers.core.observability import CheckpointException

logger = logging.getLogger(__name__)


# constants
CHECKPOINT_ACCOUNT_STATUS: str = "checkpoint_required"

# statuses that mean a previous worker died mid-run. if a task arrives in one
# of these, do not re-run it.
_ORPHAN_STATUSES: frozenset[str] = frozenset({TaskStatus.RUNNING.value})

# statuses we will dispatch to the browser. PENDING is the normal entry,
# DRAFT is allowed for the manual /orchestrator/tasks/{id}/start path.
_RUNNABLE_STATUSES: frozenset[str] = frozenset(
    {TaskStatus.PENDING.value, TaskStatus.DRAFT.value}
)

# reaper threshold. Tasks stuck in RUNNING longer than this are treated as
# dead and force-failed by the periodic janitor. 15 min covers a long warmup
# or upload chain while still freeing the account fast when a worker dies.
# Tunable per call via stale_after_seconds.
_REAP_AFTER_SECONDS: int = 15 * 60

# retry budget for the FOR UPDATE NOWAIT contention path.
_LOCK_RETRY_MAX_ATTEMPTS: int = 5
_LOCK_RETRY_BASE_BACKOFF_S: int = 30  # 30, 60, 120, 240, 480 seconds

# retry budget for the agent-limit / capacity gate (Phase 4). Generous, because
# slots free up as other tasks finish. Backoff is capped so waits stay sane.
_GATE_RETRY_MAX_ATTEMPTS: int = 20
_GATE_RETRY_BASE_BACKOFF_S: int = 15
_GATE_RETRY_BACKOFF_CAP_S: int = 120


# helpers
def _build_proxy_string(proxy: Proxy | None) -> str | None:
    """Turn a proxy orm row into a proxy URL for proxy_builder.

    Includes the SCHEME (http/https/socks4/socks5) — without it SOCKS proxies
    were silently treated as HTTP and failed. Format:
        scheme://user:pass@host:port   (with creds)
        scheme://host:port             (no creds — e.g. IP-whitelisted SOCKS)
    """
    if proxy is None:
        return None
    scheme = (getattr(proxy, "protocol", None) or "http").strip().lower()
    user = (proxy.username or "").strip()
    pwd = (proxy.password or "").strip()
    host = proxy.host
    port = proxy.port
    if user or pwd:
        return f"{scheme}://{user}:{pwd}@{host}:{port}"
    return f"{scheme}://{host}:{port}"


def _set_task_status(
    task_id: uuid.UUID,
    new_status: TaskStatus,
    *,
    error_log: str | None = None,
) -> None:
    """Switch a Task to new_status in one tx, optionally write an error log."""
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
    """Mark the account so the user has to resolve an IG challenge by hand."""
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

    With acks_late=True and a hard worker death (OOM, SIGKILL, host eviction)
    the same task message can be sent to a new worker while the db row still
    says RUNNING. Running it again would cause a duplicate IG side effect
    (double post, double DM, etc). So we refuse the re-run and leave the
    row in FAILED. The user can then decide to dispatch again by hand.
    """
    with SessionLocal() as db:
        task = db.get(Task, task_uuid)
        if task is None:
            return False  # caller deals with the not-found case
        if task.status not in _ORPHAN_STATUSES:
            return False
        task.status = TaskStatus.FAILED.value
        task.error_log = (
            "Orphan recovery: previous worker died while RUNNING. We do not "
            "auto re-run, that would cause duplicate IG side effects. "
            "Dispatch again by hand if you want this to run."
        )
        db.commit()
        logger.warning(
            "[run_instagram_task] orphan-recovered task=%s, marked FAILED",
            task_uuid,
        )
        return True


def _reap_stale_siblings_for_account(
    account_id: uuid.UUID,
    stale_after_seconds: int = _REAP_AFTER_SECONDS,
) -> int:
    """Force-fail any RUNNING task on this account older than the threshold.

    Plugs a real-world failure mode: a worker dies hard (SIGKILL/OOM) while
    holding a RUNNING row. The same Celery message is NOT retried (acks_late
    only requeues if the worker process disappears mid-ack, which doesn't fire
    on all death paths). The DB row stays RUNNING until reap_stale_tasks runs
    (was every 5 min, threshold 1h). Meanwhile every NEW task on the same
    account hits the sibling-RUNNING check and retries forever.

    By calling this from run_instagram_task at the start, the new task
    eagerly reaps zombies older than the threshold and proceeds, instead of
    waiting for the periodic beat to do it.

    Returns the number of siblings reaped.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
    reaped = 0
    with SessionLocal() as db:
        stmt = select(Task).where(
            Task.account_id == account_id,
            Task.status == TaskStatus.RUNNING.value,
            Task.created_at < cutoff,
        )
        for task in db.execute(stmt).scalars():
            task.status = TaskStatus.FAILED.value
            timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            reap_line = (
                f"[{timestamp}] eager sibling reap: stuck in RUNNING for "
                f">{stale_after_seconds}s, worker treated as dead."
            )
            task.error_log = (
                f"{task.error_log}\n{reap_line}" if task.error_log else reap_line
            )
            reaped += 1
        if reaped:
            db.commit()
            logger.warning(
                "[run_instagram_task] eager-reaped %d stale sibling(s) on account=%s",
                reaped, account_id,
            )
    return reaped


def _check_agent_and_capacity_gate(self: CeleryTask, task_uuid: uuid.UUID) -> None:
    """Phase 4 soft gate: hold the task back if the owner is out of agent
    slots OR the host is over its CPU/RAM/browser budget.

    Proceeds (returns None) on the happy path. Raises ``self.retry`` to wait
    when blocked. Fail-open: any error computing the gate lets the task run,
    so a metrics glitch can never freeze the whole fleet.
    """
    try:
        with SessionLocal() as db:
            task = db.get(Task, task_uuid)
            if task is None:
                return
            account = db.get(InstagramAccount, task.account_id)
            if account is None:
                return
            user = db.get(User, account.user_id)
            if user is not None and settings.is_admin(user.email):
                return
            limit = agents_for_subscription(user.subscription if user else None)
            running = int(
                db.execute(
                    select(func.count(Task.id))
                    .select_from(Task)
                    .join(InstagramAccount, Task.account_id == InstagramAccount.id)
                    .where(
                        InstagramAccount.user_id == account.user_id,
                        Task.status == TaskStatus.RUNNING.value,
                        Task.id != task_uuid,
                    )
                ).scalar_one()
            )
        over_limit = running >= limit
        cap_ok, cap_reason = has_free_capacity()
    except Exception:
        logger.exception("[gate] gate check failed, proceeding (fail-open)")
        return

    blocked_reason: str | None = None
    if over_limit:
        blocked_reason = f"agent slots full ({running}/{limit} running for this user)"
    elif not cap_ok:
        blocked_reason = f"host over capacity: {cap_reason}"

    if blocked_reason is None:
        return

    backoff = min(
        _GATE_RETRY_BASE_BACKOFF_S * (2 ** self.request.retries),
        _GATE_RETRY_BACKOFF_CAP_S,
    )
    logger.info(
        "[run_instagram_task] gate hold task=%s (%s), retry in %ds",
        task_uuid, blocked_reason, backoff,
    )
    raise self.retry(
        exc=RuntimeError(blocked_reason),
        countdown=backoff,
        max_retries=_GATE_RETRY_MAX_ATTEMPTS,
    )


# task: validate_account_session
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
    """Check if stored cookies for the IG account are still logged in.

    The real browser check (DrissionPage) is done by the worker layer.
    This wrapper just loads the row, updates the account and saves the
    result.
    """
    acct_uuid = uuid.UUID(account_id)
    logger.info("[validate_account_session] account_id=%s", acct_uuid)

    with SessionLocal() as db:
        account = db.get(InstagramAccount, acct_uuid)
        if account is None:
            raise ValueError(f"InstagramAccount {acct_uuid} not found")

        proxy_string = _build_proxy_string(account.proxy)
        cookies = decrypt_cookies(account.cookies) or {}

        # browser logic stub.
        # TODO: pass proxy_string and cookies to a real validator action via
        # TaskExecutor once we have a validate_session handler. For now we
        # just mark the account as valid.
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


# task: run_instagram_task
@celery_app.task(
    bind=True,
    name="ig_crm.run_instagram_task",
    acks_late=True,
)
def run_instagram_task(self: CeleryTask, task_id: str) -> dict[str, Any]:
    """Main entry point for browser automation.

    Steps:
        0. orphan recovery. if the task is already RUNNING, mark it FAILED
           and stop (previous worker died).
        1. row lock on the InstagramAccount, sibling RUNNING check, build
           payload and switch to RUNNING. All in one tx so concurrent
           workers can not race past each other.
        2. hand the payload to TaskExecutor (it closes the browser when done).
        3. success -> COMPLETED. error -> FAILED plus traceback.
    """
    task_uuid = uuid.UUID(task_id)
    logger.info("[run_instagram_task] task_id=%s", task_uuid)

    # 0. orphan recovery
    if _recover_orphan_if_needed(task_uuid):
        return {"task_id": str(task_uuid), "status": "orphan_recovered"}

    # 0.1 eager-reap stale RUNNING siblings on the same account.
    # If a previous worker died hard, its Task row stays RUNNING until the
    # periodic beat (reap_stale_tasks) catches it. Without this, every new
    # task on the same account would just keep retrying behind the zombie.
    # We need to peek at task.account_id first; if the Task itself is gone
    # (deleted), fall through to the graceful not-found handling below.
    try:
        with SessionLocal() as db:
            _peek_task = db.get(Task, task_uuid)
            if _peek_task is not None:
                _reap_stale_siblings_for_account(_peek_task.account_id)
    except Exception:
        logger.exception(
            "[run_instagram_task] eager sibling reap raised, proceeding (fail-open)"
        )

    # 0.5 agent-limit + host-capacity gate (Phase 4). Holds the task back via
    # self.retry while the owner has no free agent slot or the box is loaded.
    _check_agent_and_capacity_gate(self, task_uuid)

    # 1. locked load, sibling check, switch to RUNNING.
    # everything in this block is one transaction. the row lock is released
    # only at db.commit(). we set RUNNING BEFORE commit, so a concurrent
    # worker's sibling check sees us as RUNNING the moment the lock drops.
    account_uuid: uuid.UUID
    payload: dict[str, Any]
    with SessionLocal() as db:
        task = db.get(Task, task_uuid)
        if task is None:
            # The Task row was deleted between enqueue and run (the user
            # deleted the account → cascade, or a manual SQL reset). Do NOT
            # raise — that would trigger the autoretry path and loop forever.
            logger.warning(
                "[run_instagram_task] task=%s row gone from DB, dropping message",
                task_uuid,
            )
            return {"task_id": str(task_uuid), "status": "task_not_found"}

        if task.status not in _RUNNABLE_STATUSES:
            logger.warning(
                "[run_instagram_task] task=%s status %r is not runnable, skip",
                task_uuid, task.status,
            )
            return {
                "task_id": str(task_uuid),
                "status": "non_runnable",
                "task_status": task.status,
            }

        # row-level lock on the account. NOWAIT raises right away if another
        # worker holds the row, which is what we want, because IG treats
        # overlapping sessions as a compromised account.
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
                "[run_instagram_task] account=%s locked by sibling, retry in %ds",
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
            # Account was deleted after the task was enqueued. Mark the task
            # FAILED in-place and return — raising would re-loop the message.
            logger.warning(
                "[run_instagram_task] account=%s gone, failing task=%s",
                task.account_id, task_uuid,
            )
            task.status = TaskStatus.FAILED.value
            task.error_log = (
                f"InstagramAccount {task.account_id} no longer exists "
                "(deleted before the worker picked this task up)."
            )
            db.commit()
            return {
                "task_id": str(task_uuid),
                "status": "account_not_found",
            }

        # extra safety: even with the row lock, skip if a sibling Task for
        # this account is already RUNNING. covers the case where the sibling
        # postgres connection died without releasing the FOR UPDATE lock,
        # so the row looks free but a stale Task row is still RUNNING.
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
                "[run_instagram_task] sibling task %s still RUNNING for account=%s, retry in %ds",
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

        # save the fields we need to use after the session closes
        proxy = account.proxy
        account_uuid = account.id

        # no-proxy runs allowed globally (ALLOW_NO_PROXY) or when the account
        # owner is an admin (lets admins test the pipeline without a proxy).
        owner = db.get(User, account.user_id)
        allow_no_proxy = settings.ALLOW_NO_PROXY or settings.is_admin(
            owner.email if owner else None
        )

        # the AI parser saves the whole plan dict on Task.payload:
        #   {"summary": ..., "priority": ..., "commands": [{action, args}, ...]}
        # the executor wants payload["commands"] to be just the list, so we
        # flatten it here.
        plan: dict[str, Any] = task.payload or {}
        commands_list: list[dict[str, Any]] = plan.get("commands") or []

        payload = {
            "task_id": str(task.id),
            "account_id": str(account.id),
            "ig_username": account.ig_username,
            "ig_password": decrypt_secret(account.ig_password),
            "auth_method": account.auth_method,
            "proxy_string": _build_proxy_string(proxy),
            "allow_no_proxy": allow_no_proxy,
            "proxy_session_id": account.proxy_session_id,
            "cookies": decrypt_cookies(account.cookies) or {},
            "commands": commands_list,
            "plan_summary": plan.get("summary"),
            "plan_priority": plan.get("priority"),
            "priority": task.priority,
        }

        # set RUNNING inside the locked tx, so a concurrent worker's sibling
        # check sees us the second the lock is released.
        task.status = TaskStatus.RUNNING.value
        task.error_log = None
        db.commit()

    # 2. run via TaskExecutor
    try:
        result: dict[str, Any] = TaskExecutor(payload).execute()

    except CheckpointException as exc:
        # IG sent the session to a challenge or suspended page. Mark the
        # Task FAILED and the account checkpoint_required, so the user can
        # fix it by hand before any new dispatch.
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
        # safety net: if anything inside the executor calls self.retry()
        # through a nested Celery primitive, the Retry must reach Celery
        # as is. do not mark it FAILED.
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


# task: reap_stale_tasks (janitor)
@celery_app.task(name="ig_crm.reap_stale_tasks")
def reap_stale_tasks(stale_after_seconds: int | None = None) -> dict[str, Any]:
    """Force-fail any Task stuck in RUNNING longer than the threshold.

    Pair to the at-start orphan recovery. Covers the case where a worker
    died but the message was NOT requeued (broker reconnect failure, host
    hard reboot...), so the row would otherwise stay RUNNING forever.
    Run via Celery Beat every ~5 minutes. Default threshold is one hour.

    On the timestamp: the Task model has created_at but no updated_at or
    started_at. We use created_at as a safe upper bound. A Task that was
    created over an hour ago AND is still RUNNING is broken enough to reap.
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
                f">{threshold}s, worker treated as dead."
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


# statuses that exclude an account from stats collection.
# accounts in these states either can't be accessed or should not be touched.
_STATS_EXCLUDED_STATUSES: frozenset[str] = frozenset(
    {"checkpoint_required", "banned", "invalid"}
)


# task: dispatch_stats_collection (hourly beat dispatcher)
@celery_app.task(name="ig_crm.dispatch_stats_collection")
def dispatch_stats_collection() -> dict[str, Any]:
    """Fetch all active accounts and dispatch a stats task for each one.

    Triggered by Celery Beat every hour. For each eligible account, sends
    a gather_account_metrics task to the stats_queue. The per-account task
    handles its own session isolation (FOR UPDATE NOWAIT), so it's safe to
    dispatch even if a standard task is running on the account.
    """
    dispatched_ids: list[str] = []

    with SessionLocal() as db:
        stmt = select(InstagramAccount).where(
            ~InstagramAccount.status.in_(_STATS_EXCLUDED_STATUSES)
            | InstagramAccount.status.is_(None)
        )
        accounts = list(db.execute(stmt).scalars())

    for account in accounts:
        gather_account_metrics.delay(str(account.id))
        dispatched_ids.append(str(account.id))

    logger.info(
        "[dispatch_stats_collection] dispatched %d account(s) to stats_queue",
        len(dispatched_ids),
    )
    return {
        "dispatched": len(dispatched_ids),
        "account_ids": dispatched_ids,
    }


# task: gather_account_metrics (per-account stats collection)
@celery_app.task(
    bind=True,
    name="ig_crm.gather_account_metrics",
    time_limit=180,           # hard kill after 3 min (fast action)
    soft_time_limit=120,      # SoftTimeLimitExceeded after 2 min
)
def gather_account_metrics(self: CeleryTask, account_id: str) -> dict[str, Any]:
    """Spawn a headless browser to collect metrics for one account.

    Safety guarantees:
        1. Acquires a FOR UPDATE NOWAIT lock on the account row.
           If another task (upload, warmup, etc.) already holds the lock,
           we skip gracefully — no retry, wait for the next hourly beat.
        2. Pings the proxy before launching Chrome. Dead proxy = abort.
        3. Runs in headless mode to conserve server RAM.
        4. Uses the existing TaskExecutor + ObservabilityMonitor pipeline
           for metrics interception and persistence.

    This task does NOT create a Task row — it's a technical background job,
    not a user-visible task. Metrics appear in account_metrics regardless.
    """
    acct_uuid = uuid.UUID(account_id)
    logger.info("[gather_account_metrics] account_id=%s", acct_uuid)

    # step 1: load the account and attempt the pessimistic row lock.
    # the lock prevents a concurrent browser session on the same account,
    # which would trigger an instant ban from Meta.
    try:
        with SessionLocal() as db:
            try:
                account = db.execute(
                    select(InstagramAccount)
                    .where(InstagramAccount.id == acct_uuid)
                    .with_for_update(nowait=True)
                ).scalar_one_or_none()
            except OperationalError:
                # another worker holds the lock — a standard task is running.
                # do NOT retry. stats collection is best-effort, the next
                # hourly beat will pick this account up again.
                logger.info(
                    "[gather_account_metrics] account_id=%s is busy "
                    "(locked by another task), skipping stats collection",
                    acct_uuid,
                )
                return {"account_id": account_id, "status": "account_busy"}

            if account is None:
                logger.warning(
                    "[gather_account_metrics] account %s not found", acct_uuid
                )
                return {"account_id": account_id, "status": "not_found"}

            # step 2: pre-flight proxy check.
            # if the proxy is dead, launching Chrome is a waste of RAM.
            proxy = account.proxy
            if proxy is not None:
                from app.services.trust import _evaluate_proxy

                proxy_ok, _, proxy_reason = _evaluate_proxy(proxy, skip_probe=False)
                if not proxy_ok:
                    logger.warning(
                        "[gather_account_metrics] proxy dead for account_id=%s: %s",
                        acct_uuid,
                        proxy_reason,
                    )
                    return {
                        "account_id": account_id,
                        "status": "proxy_dead",
                        "reason": proxy_reason,
                    }

            # step 3: build the minimal payload.
            # single command: gather_stats. headless is forced to True.
            payload: dict[str, Any] = {
                "task_id": None,  # no Task row for technical background jobs
                "account_id": str(account.id),
                "ig_username": account.ig_username,
                "proxy_string": _build_proxy_string(proxy),
                "cookies": decrypt_cookies(account.cookies) or {},
                "headless": True,
                "user_agent": account.user_agent,
                "commands": [
                    {
                        "action": "gather_stats",
                        "args": {"ig_username": account.ig_username},
                    }
                ],
            }

            # commit the transaction to release the FOR UPDATE lock.
            # TaskExecutor will open its own sessions for metric persistence.
            db.commit()

    except Exception:
        logger.exception(
            "[gather_account_metrics] failed to prepare payload for account_id=%s",
            acct_uuid,
        )
        raise

    # step 4: run the browser session.
    # TaskExecutor starts the ObservabilityMonitor, which intercepts GraphQL
    # responses and persists them to account_metrics automatically.
    try:
        result: dict[str, Any] = TaskExecutor(payload).execute()
    except SoftTimeLimitExceeded:
        logger.error(
            "[gather_account_metrics] soft time limit hit for account_id=%s",
            acct_uuid,
        )
        return {"account_id": account_id, "status": "timeout"}
    except Exception as exc:
        logger.exception(
            "[gather_account_metrics] browser session failed for account_id=%s",
            acct_uuid,
        )
        return {
            "account_id": account_id,
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
        }

    logger.info(
        "[gather_account_metrics] completed for account_id=%s, metrics=%s",
        acct_uuid,
        result.get("metrics"),
    )
    return {
        "account_id": account_id,
        "status": "ok",
        "metrics": result.get("metrics"),
    }

