"""Security & Phase-2/4 tests: secret encryption, agent accounting, the
capacity gate, admin allow-list, and per-user account scoping.

Run: cd backend && ../.venv/bin/python -m pytest tests/test_security.py -v
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import make_fake_account


# --- secret encryption -----------------------------------------------------
class TestEncryption:
    def test_password_round_trip(self):
        from app.core.secrets import decrypt_secret, encrypt_secret

        enc = encrypt_secret("hunter2")
        assert enc != "hunter2"
        assert enc.startswith("enc::")
        assert "hunter2" not in enc
        assert decrypt_secret(enc) == "hunter2"

    def test_empty_and_none_pass_through(self):
        from app.core.secrets import encrypt_secret

        assert encrypt_secret("") == ""
        assert encrypt_secret(None) is None

    def test_legacy_plaintext_decrypts_to_itself(self):
        # rows written before encryption have no marker -> returned as-is
        from app.core.secrets import decrypt_secret

        assert decrypt_secret("oldplaintextpw") == "oldplaintextpw"

    def test_cookies_envelope_round_trip(self):
        from app.core.secrets import decrypt_cookies, encrypt_cookies

        cookies = [{"name": "sessionid", "value": "SECRET"}]
        env = encrypt_cookies(cookies)
        assert "SECRET" not in str(env)
        assert "_enc" in env
        assert decrypt_cookies(env) == cookies

    def test_cookies_none_and_legacy(self):
        from app.core.secrets import decrypt_cookies, encrypt_cookies

        assert encrypt_cookies(None) is None
        legacy = [{"name": "x", "value": "y"}]
        assert decrypt_cookies(legacy) == legacy  # no _enc marker -> as-is


# --- agent slot accounting -------------------------------------------------
class TestAgents:
    def test_tier_defaults(self):
        from app.services.agents import tier_default_agents

        assert tier_default_agents("free") == 1
        assert tier_default_agents("pro") == 5
        assert tier_default_agents("enterprise") == 10
        assert tier_default_agents(None) == 1
        assert tier_default_agents("bogus") == 1

    def test_explicit_override_wins(self):
        from types import SimpleNamespace

        from app.services.agents import agents_for_subscription

        sub = SimpleNamespace(tier="free", agents_limit=42)
        assert agents_for_subscription(sub) == 42

    def test_none_subscription_is_free(self):
        from app.services.agents import agents_for_subscription

        assert agents_for_subscription(None) == 1

    def test_negative_override_clamped(self):
        from types import SimpleNamespace

        from app.services.agents import agents_for_subscription

        sub = SimpleNamespace(tier="pro", agents_limit=-5)
        assert agents_for_subscription(sub) == 0


# --- capacity gate ---------------------------------------------------------
class TestCapacityGate:
    def test_blocks_on_high_cpu(self, mocker):
        from app.services import capacity

        mocker.patch.object(capacity.psutil, "cpu_percent", return_value=99.0)
        mocker.patch.object(
            capacity.psutil, "virtual_memory",
            return_value=mocker.Mock(percent=10.0),
        )
        mocker.patch.object(capacity, "count_our_browsers", return_value=0)
        ok, reason = capacity.has_free_capacity()
        assert ok is False and "cpu" in reason.lower()

    def test_blocks_on_browser_ceiling(self, mocker):
        from app.services import capacity

        mocker.patch.object(capacity.psutil, "cpu_percent", return_value=1.0)
        mocker.patch.object(
            capacity.psutil, "virtual_memory",
            return_value=mocker.Mock(percent=1.0),
        )
        mocker.patch.object(
            capacity, "count_our_browsers",
            return_value=capacity.settings.CAPACITY_MAX_BROWSERS,
        )
        ok, reason = capacity.has_free_capacity()
        assert ok is False and "browser" in reason.lower()

    def test_ok_when_idle(self, mocker):
        from app.services import capacity

        mocker.patch.object(capacity.psutil, "cpu_percent", return_value=2.0)
        mocker.patch.object(
            capacity.psutil, "virtual_memory",
            return_value=mocker.Mock(percent=3.0),
        )
        mocker.patch.object(capacity, "count_our_browsers", return_value=0)
        ok, _ = capacity.has_free_capacity()
        assert ok is True

    def test_fails_open_on_probe_error(self, mocker):
        from app.services import capacity

        mocker.patch.object(
            capacity.psutil, "cpu_percent", side_effect=RuntimeError("boom")
        )
        ok, reason = capacity.has_free_capacity()
        assert ok is True  # fail-open: never block the fleet on a metrics glitch


# --- admin allow-list ------------------------------------------------------
class TestIsAdmin:
    def test_csv_membership(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "ADMIN_EMAILS", "boss@x.com, ME@Y.com")
        assert settings.is_admin("boss@x.com") is True
        assert settings.is_admin("me@y.com") is True       # case-insensitive
        assert settings.is_admin("intruder@z.com") is False
        assert settings.is_admin(None) is False

    def test_empty_allowlist_denies_all(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "ADMIN_EMAILS", "")
        assert settings.is_admin("anyone@x.com") is False


# --- account create is scoped to the token user (no spoofing) --------------
class TestAccountOwnershipFromToken:
    def test_body_user_id_is_overridden_by_token(self, client, current_user, mocker):
        spoofed = uuid.uuid4()
        fake = make_fake_account(user_id=current_user.id, ig_username="scoped")
        create_mock = mocker.patch(
            "app.api.routers.account.crud_account.create_account",
            return_value=fake,
        )

        resp = client.post(
            "/accounts/",
            json={
                "user_id": str(spoofed),  # attacker tries to assign to someone else
                "ig_username": "scoped",
                "ig_password": "p",
                "auth_method": "cookies",
            },
        )

        assert resp.status_code == 201, resp.text
        # the schema handed to CRUD must carry the TOKEN's user id, not the body's
        passed_schema = create_mock.call_args[0][1]
        assert passed_schema.user_id == current_user.id
        assert passed_schema.user_id != spoofed


# --- read schema never exposes secrets -------------------------------------
class TestReadSchemaHasNoSecrets:
    def test_account_read_omits_password_and_cookies(self):
        from app.schemas.account import InstagramAccountRead

        fields = set(InstagramAccountRead.model_fields.keys())
        assert "ig_password" not in fields
        assert "cookies" not in fields
