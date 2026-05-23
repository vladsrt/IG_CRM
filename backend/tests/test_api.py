"""
api tests.
tests accounts and orchestrator routes with mocked db and celery.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from tests.conftest import (
    failing_trust_report,
    make_fake_account,
    make_fake_task,
    passing_trust_report,
)


# post /accounts/
class TestCreateAccount:
    def test_happy_path_returns_201_and_serialized_account(self, client, mocker):
        user_id = uuid.uuid4()
        fake_account = make_fake_account(
            user_id=user_id,
            ig_username="cryptoking",
            tags=["crypto", "tier1"],
        )
        create_mock = mocker.patch(
            "app.api.routers.account.crud_account.create_account",
            return_value=fake_account,
        )

        response = client.post(
            "/accounts/",
            json={
                "user_id": str(user_id),
                "ig_username": "cryptoking",
                "ig_password": "supersecret123",
                "auth_method": "cookies",
                "tags": ["crypto", "tier1"],
            },
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["ig_username"] == "cryptoking"
        assert body["tags"] == ["crypto", "tier1"]
        assert body["user_id"] == str(user_id)
        # CRUD was actually invoked exactly once with our schema instance
        assert create_mock.call_count == 1

    def test_invalid_auth_method_returns_422(self, client):
        response = client.post(
            "/accounts/",
            json={
                "user_id": str(uuid.uuid4()),
                "ig_username": "u",
                "ig_password": "p",
                "auth_method": "bogus_method",
            },
        )
        assert response.status_code == 422
        # Pydantic's enum validation surfaces the field name
        assert any(
            "auth_method" in str(err.get("loc", ""))
            for err in response.json()["detail"]
        )

    def test_missing_required_field_returns_422(self, client):
        # Missing ig_password
        response = client.post(
            "/accounts/",
            json={
                "user_id": str(uuid.uuid4()),
                "ig_username": "u",
                "auth_method": "cookies",
            },
        )
        assert response.status_code == 422

    def test_crud_value_error_maps_to_400(self, client, mocker):
        mocker.patch(
            "app.api.routers.account.crud_account.create_account",
            side_effect=ValueError("User a1b2c3 does not exist"),
        )

        response = client.post(
            "/accounts/",
            json={
                "user_id": str(uuid.uuid4()),
                "ig_username": "u",
                "ig_password": "supersecret",
                "auth_method": "cookies",
            },
        )
        assert response.status_code == 400
        assert "does not exist" in response.json()["detail"]


# post /orchestrator/tasks/fan-out
class TestFanOut:
    """tests for orchestrator tasks fan-out."""

    @staticmethod
    def _valid_plan(commands=None, tags=("crypto",), clarification=None):
        return {
            "summary": "post the new reel to crypto accounts",
            "priority": "normal",
            "target_tags": list(tags),
            "clarification_needed": clarification,
            "commands": commands if commands is not None else [
                {"action": "warmup", "args": []}
            ],
        }

    def test_dispatch_creates_task_and_calls_celery(self, client, mocker):
        account = make_fake_account(tags=["crypto"])
        task = make_fake_task(account_id=account.id, status="pending")
        async_result = SimpleNamespace(id="celery-task-abc123")

        mocker.patch(
            "app.api.routers.orchestrator.crud_account.list_accounts_by_tags",
            return_value=[account],
        )
        mocker.patch(
            "app.api.routers.orchestrator._evaluate_account_trust",
            return_value=passing_trust_report(),
        )
        # Bypass the spintax pass — test inputs don't have spintax.
        mocker.patch(
            "app.api.routers.orchestrator.uniqueize_plan_payload",
            side_effect=lambda payload: payload,
        )
        create_task_mock = mocker.patch(
            "app.api.routers.orchestrator.crud_task.create_task",
            return_value=task,
        )
        run_task_mock = mocker.patch(
            "app.api.routers.orchestrator.run_instagram_task"
        )
        run_task_mock.delay.return_value = async_result

        response = client.post(
            "/orchestrator/tasks/fan-out",
            json={"plan": self._valid_plan(), "target_account_ids": []},
        )

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["dispatched_count"] == 1
        assert body["skipped_count"] == 0
        assert body["dispatched"][0]["celery_task_id"] == "celery-task-abc123"
        assert body["dispatched"][0]["account_id"] == str(account.id)
        assert body["celery_task_ids"] == ["celery-task-abc123"]

        # The Celery task was actually dispatched with the new task UUID.
        run_task_mock.delay.assert_called_once_with(str(task.id))
        # And the Task row was created with the right account_id.
        assert create_task_mock.call_count == 1
        created_arg = create_task_mock.call_args[0][1]
        assert created_arg.account_id == account.id

    def test_clarification_needed_short_circuits_with_400(self, client):
        response = client.post(
            "/orchestrator/tasks/fan-out",
            json={
                "plan": self._valid_plan(
                    commands=[],
                    clarification="What time should I post?",
                ),
                "target_account_ids": [str(uuid.uuid4())],
            },
        )
        assert response.status_code == 400
        # the route refuses with a "needs more info / Question:" message
        detail = response.json()["detail"].lower()
        assert "more info" in detail or "question" in detail

    def test_empty_commands_short_circuits_with_400(self, client):
        response = client.post(
            "/orchestrator/tasks/fan-out",
            json={
                "plan": self._valid_plan(commands=[]),
                "target_account_ids": [str(uuid.uuid4())],
            },
        )
        assert response.status_code == 400
        assert "no commands" in response.json()["detail"].lower()

    def test_no_resolved_accounts_returns_400(self, client, mocker):
        mocker.patch(
            "app.api.routers.orchestrator.crud_account.list_accounts_by_tags",
            return_value=[],
        )
        mocker.patch(
            "app.api.routers.orchestrator.crud_account.get_account",
            return_value=None,
        )

        response = client.post(
            "/orchestrator/tasks/fan-out",
            json={
                "plan": self._valid_plan(tags=["nonexistent"]),
                "target_account_ids": [],
            },
        )
        assert response.status_code == 400
        assert "no target accounts" in response.json()["detail"].lower()

    def test_trust_gate_skips_account_without_dispatch(self, client, mocker):
        account = make_fake_account(tags=["crypto"])

        mocker.patch(
            "app.api.routers.orchestrator.crud_account.list_accounts_by_tags",
            return_value=[account],
        )
        mocker.patch(
            "app.api.routers.orchestrator._evaluate_account_trust",
            return_value=failing_trust_report("proxy probe failed: Connection refused"),
        )
        # If the trust gate works, neither of these should run.
        create_mock = mocker.patch(
            "app.api.routers.orchestrator.crud_task.create_task"
        )
        run_task_mock = mocker.patch(
            "app.api.routers.orchestrator.run_instagram_task"
        )

        response = client.post(
            "/orchestrator/tasks/fan-out",
            json={"plan": self._valid_plan(), "target_account_ids": []},
        )

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["dispatched_count"] == 0
        assert body["skipped_count"] == 1
        assert body["skipped"][0]["account_id"] == str(account.id)
        assert "low_trust_score" in body["skipped"][0]["reason"]

        create_mock.assert_not_called()
        run_task_mock.delay.assert_not_called()

    def test_missing_explicit_account_reported_in_skipped(self, client, mocker):
        ghost_id = uuid.uuid4()
        real_account = make_fake_account()
        real_task = make_fake_task(account_id=real_account.id)

        mocker.patch(
            "app.api.routers.orchestrator.crud_account.list_accounts_by_tags",
            return_value=[real_account],
        )
        mocker.patch(
            "app.api.routers.orchestrator.crud_account.get_account",
            return_value=None,  # ghost_id doesn't exist
        )
        mocker.patch(
            "app.api.routers.orchestrator._evaluate_account_trust",
            return_value=passing_trust_report(),
        )
        mocker.patch(
            "app.api.routers.orchestrator.uniqueize_plan_payload",
            side_effect=lambda p: p,
        )
        mocker.patch(
            "app.api.routers.orchestrator.crud_task.create_task",
            return_value=real_task,
        )
        run_task_mock = mocker.patch(
            "app.api.routers.orchestrator.run_instagram_task"
        )
        run_task_mock.delay.return_value = SimpleNamespace(id="celery-real")

        response = client.post(
            "/orchestrator/tasks/fan-out",
            json={
                "plan": self._valid_plan(tags=["crypto"]),
                "target_account_ids": [str(ghost_id)],
            },
        )

        assert response.status_code == 202, response.text
        body = response.json()
        # The real one dispatched
        assert body["dispatched_count"] == 1
        # The ghost reported as skipped with a clear reason
        skipped_ids = {s["account_id"] for s in body["skipped"]}
        assert str(ghost_id) in skipped_ids
        ghost_skip = next(s for s in body["skipped"] if s["account_id"] == str(ghost_id))
        assert "not found" in ghost_skip["reason"].lower()
