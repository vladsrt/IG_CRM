"""
celery worker tests.
tests task states without running browser.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
from celery.exceptions import Retry
from sqlalchemy.exc import OperationalError

from app.models.account import InstagramAccount
from app.models.task import Task, TaskStatus
from tests.conftest import make_fake_account, make_fake_task, make_mock_session


def _patch_session_factory(mocker, *, fake_task, fake_account, sibling=None):
    """patches session to return mock."""
    cm, db = make_mock_session(
        get_returns={Task: fake_task, InstagramAccount: fake_account},
        execute_results=[
            MagicMock(scalar_one_or_none=MagicMock(return_value=fake_account)),
            MagicMock(scalar_one_or_none=MagicMock(return_value=sibling)),
        ],
    )
    mocker.patch("app.workers.celery_tasks.SessionLocal", return_value=cm)
    return cm, db


# happy path
class TestRunInstagramTaskHappyPath:
    def test_pending_task_transitions_running_then_completed(self, mocker):
        task = make_fake_task(status=TaskStatus.PENDING.value)
        account = make_fake_account(id=task.account_id)

        _patch_session_factory(mocker, fake_task=task, fake_account=account)
        set_status_mock = mocker.patch(
            "app.workers.celery_tasks._set_task_status"
        )
        executor_class = mocker.patch(
            "app.workers.celery_tasks.TaskExecutor"
        )
        executor_class.return_value.execute.return_value = {
            "task_id": str(task.id),
            "status": "ok",
            "executed": 1,
            "results": [],
        }

        from app.workers.celery_tasks import run_instagram_task
        result = run_instagram_task.apply(args=[str(task.id)], throw=True).get()

        # Inside the locked transaction, run_instagram_task mutates the
        # task.status directly to RUNNING — fake_task is the same object.
        assert task.status == TaskStatus.RUNNING.value
        assert task.error_log is None

        # The executor was constructed with our payload AND .execute() ran.
        executor_class.assert_called_once()
        executor_class.return_value.execute.assert_called_once_with()

        # COMPLETED transition uses the helper (separate session).
        completed_calls = [
            c for c in set_status_mock.call_args_list
            if c.args[1] == TaskStatus.COMPLETED
        ]
        assert len(completed_calls) == 1, (
            f"expected exactly one COMPLETED transition, "
            f"got: {set_status_mock.call_args_list}"
        )

        assert result["status"] == "ok"

    def test_payload_handed_to_executor_contains_account_credentials(
        self, mocker
    ):
        """payload must have ig credentials and proxy."""
        task = make_fake_task(
            status=TaskStatus.PENDING.value,
            payload={
                "summary": "warmup",
                "priority": "normal",
                "commands": [{"action": "warmup", "args": {}}],
            },
        )
        account = make_fake_account(
            id=task.account_id,
            ig_username="puppet42",
            ig_password="pw-hashed",
            cookies=[{"name": "sessionid", "value": "abc"}],
        )

        _patch_session_factory(mocker, fake_task=task, fake_account=account)
        mocker.patch("app.workers.celery_tasks._set_task_status")
        executor_class = mocker.patch(
            "app.workers.celery_tasks.TaskExecutor"
        )
        executor_class.return_value.execute.return_value = {"status": "ok"}

        from app.workers.celery_tasks import run_instagram_task
        run_instagram_task.apply(args=[str(task.id)], throw=True).get()

        payload = executor_class.call_args[0][0]
        assert payload["task_id"] == str(task.id)
        assert payload["account_id"] == str(account.id)
        assert payload["ig_username"] == "puppet42"
        assert payload["ig_password"] == "pw-hashed"
        assert payload["commands"] == [{"action": "warmup", "args": {}}]


# orphan recovery
class TestOrphanRecovery:
    def test_running_task_is_refused_and_marked_failed(self, mocker):
        """task arriving as running should be marked failed."""
        task = make_fake_task(status=TaskStatus.RUNNING.value)
        account = make_fake_account(id=task.account_id)

        # Orphan recovery uses the same SessionLocal patch as the main path.
        # It only calls db.get (no execute), so we don't need execute_results
        # past what's already there — we won't reach the second SessionLocal.
        _patch_session_factory(mocker, fake_task=task, fake_account=account)

        executor_class = mocker.patch(
            "app.workers.celery_tasks.TaskExecutor"
        )

        from app.workers.celery_tasks import run_instagram_task
        result = run_instagram_task.apply(args=[str(task.id)], throw=True).get()

        # Orphan path returns a sentinel result and never invokes the executor.
        assert result == {"task_id": str(task.id), "status": "orphan_recovered"}
        executor_class.assert_not_called()

        # The orphan path mutates task.status to FAILED via the same db session.
        assert task.status == TaskStatus.FAILED.value
        assert task.error_log is not None
        assert "Orphan recovery" in task.error_log

    def test_completed_task_is_treated_as_non_runnable(self, mocker):
        """completed tasks should not run again."""
        task = make_fake_task(status=TaskStatus.COMPLETED.value)
        account = make_fake_account(id=task.account_id)
        _patch_session_factory(mocker, fake_task=task, fake_account=account)

        executor_class = mocker.patch(
            "app.workers.celery_tasks.TaskExecutor"
        )

        from app.workers.celery_tasks import run_instagram_task
        result = run_instagram_task.apply(args=[str(task.id)], throw=True).get()

        assert result["status"] == "non_runnable"
        assert result["task_status"] == TaskStatus.COMPLETED.value
        executor_class.assert_not_called()


# failure paths
class TestFailurePaths:
    def test_executor_exception_marks_task_failed(self, mocker):
        task = make_fake_task(status=TaskStatus.PENDING.value)
        account = make_fake_account(id=task.account_id)

        _patch_session_factory(mocker, fake_task=task, fake_account=account)
        set_status_mock = mocker.patch(
            "app.workers.celery_tasks._set_task_status"
        )
        executor_class = mocker.patch(
            "app.workers.celery_tasks.TaskExecutor"
        )
        executor_class.return_value.execute.side_effect = RuntimeError(
            "browser-side detonation"
        )

        from app.workers.celery_tasks import run_instagram_task
        with pytest.raises(RuntimeError, match="browser-side detonation"):
            run_instagram_task.apply(args=[str(task.id)], throw=True).get()

        # The task was marked FAILED with a traceback.
        failed_calls = [
            c for c in set_status_mock.call_args_list
            if c.args[1] == TaskStatus.FAILED
        ]
        assert len(failed_calls) == 1
        assert "browser-side detonation" in failed_calls[0].kwargs["error_log"]

    def test_checkpoint_exception_flags_account(self, mocker):
        task = make_fake_task(status=TaskStatus.PENDING.value)
        account = make_fake_account(id=task.account_id)

        _patch_session_factory(mocker, fake_task=task, fake_account=account)
        set_status_mock = mocker.patch(
            "app.workers.celery_tasks._set_task_status"
        )
        mark_account_mock = mocker.patch(
            "app.workers.celery_tasks._mark_account_checkpoint"
        )

        from workers.core.observability import CheckpointException
        executor_class = mocker.patch(
            "app.workers.celery_tasks.TaskExecutor"
        )
        executor_class.return_value.execute.side_effect = CheckpointException(
            "https://www.instagram.com/challenge/"
        )

        from app.workers.celery_tasks import run_instagram_task
        with pytest.raises(CheckpointException):
            run_instagram_task.apply(args=[str(task.id)], throw=True).get()

        # Task → FAILED with the checkpoint URL.
        failed_calls = [
            c for c in set_status_mock.call_args_list
            if c.args[1] == TaskStatus.FAILED
        ]
        assert len(failed_calls) == 1
        assert "/challenge/" in failed_calls[0].kwargs["error_log"]

        # Account → checkpoint_required.
        mark_account_mock.assert_called_once_with(
            account.id, "https://www.instagram.com/challenge/"
        )


# lock contention
class TestLockContention:
    def test_for_update_nowait_failure_triggers_retry(self, mocker):
        """retries if account row is locked."""
        task = make_fake_task(status=TaskStatus.PENDING.value)
        account = make_fake_account(id=task.account_id)

        # Stage db.execute to RAISE OperationalError on the FOR UPDATE query.
        cm, db = make_mock_session(
            get_returns={Task: task, InstagramAccount: account},
            execute_results=[OperationalError("SELECT FOR UPDATE", {}, Exception("locked"))],
        )
        mocker.patch("app.workers.celery_tasks.SessionLocal", return_value=cm)

        executor_class = mocker.patch(
            "app.workers.celery_tasks.TaskExecutor"
        )

        from app.workers.celery_tasks import run_instagram_task
        # Celery's self.retry() raises celery.exceptions.Retry. With throw=True
        # on the EagerResult, that propagates here.
        with pytest.raises(Retry):
            run_instagram_task.apply(args=[str(task.id)], throw=True).get()

        # The browser was NEVER touched on contention.
        executor_class.assert_not_called()

    def test_sibling_running_task_triggers_retry(self, mocker):
        """retries if another task is running for the account."""
        task = make_fake_task(status=TaskStatus.PENDING.value)
        account = make_fake_account(id=task.account_id)
        sibling_id = uuid.uuid4()

        # FOR UPDATE succeeds → returns account; sibling check → returns a UUID.
        cm, db = make_mock_session(
            get_returns={Task: task, InstagramAccount: account},
            execute_results=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=account)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=sibling_id)),
            ],
        )
        mocker.patch("app.workers.celery_tasks.SessionLocal", return_value=cm)

        executor_class = mocker.patch(
            "app.workers.celery_tasks.TaskExecutor"
        )

        from app.workers.celery_tasks import run_instagram_task
        with pytest.raises(Retry):
            run_instagram_task.apply(args=[str(task.id)], throw=True).get()

        executor_class.assert_not_called()
