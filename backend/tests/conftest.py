"""
shared fixtures for tests.
we mock postgres, redis, openai and browsers.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest


# sys path setup
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))


# env variables
os.environ.setdefault("OPENAI_API_KEY", "sk-test-suite")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")
os.environ.setdefault("MEDIA_ROOT", "/tmp/ig-crm-test-media")


# test factories
def make_fake_account(**overrides: Any) -> SimpleNamespace:
    """returns a fake account."""
    base: dict[str, Any] = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        proxy_id=None,
        proxy=None,
        ig_username="testuser",
        ig_password="hashed-pw",
        auth_method="cookies",
        proxy_session_id=None,
        cookies=None,
        status=None,
        error_log=None,
        last_check=None,
        tags=[],
        platform="windows",
        user_agent=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def make_fake_proxy(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        id=uuid.uuid4(),
        host="proxy.example.com",
        port=8080,
        username="puser",
        password="ppass",
        rotation_url="https://rotate.example.com/x",
        type="ordinary",
        protocol="http",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def make_fake_task(**overrides: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        status="pending",
        payload={"summary": "test", "priority": "normal", "commands": []},
        priority=5,
        error_log=None,
        created_at=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def make_mock_session(
    *,
    get_returns: dict[Any, Any] | None = None,
    execute_results: list[Any] | None = None,
) -> tuple[MagicMock, MagicMock]:
    """returns a mock db session."""
    db = MagicMock(name="MockSession")
    if get_returns is not None:
        db.get.side_effect = lambda model, _id: get_returns.get(model)
    if execute_results is not None:
        db.execute.side_effect = list(execute_results)

    cm = MagicMock(name="MockSessionCM")
    cm.__enter__.return_value = db
    cm.__exit__.return_value = False
    return cm, db


def passing_trust_report():
    """returns a passing trust report."""
    from app.services.trust import TrustReport
    return TrustReport(
        score=80,
        proxy_ok=True,
        proxy_latency_ms=42,
        user_agent_ok=True,
        hygiene_ok=True,
        reasons=[],
    )


def failing_trust_report(reason: str = "proxy probe failed"):
    """returns a failing trust report."""
    from app.services.trust import TrustReport
    return TrustReport(
        score=15,
        proxy_ok=False,
        proxy_latency_ms=None,
        user_agent_ok=True,
        hygiene_ok=False,
        reasons=[reason],
    )


# fastapi test client
@pytest.fixture
def client():
    """test client with fake db."""
    from fastapi.testclient import TestClient

    from app.api.main import app
    from app.core.database import get_db

    def _fake_get_db():
        yield MagicMock(name="RouteDBSession")

    app.dependency_overrides[get_db] = _fake_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


# mock browser
@pytest.fixture
def mocked_browser_class(mocker):
    """replaces InstagramBrowser for testing."""
    mock_class = MagicMock(name="InstagramBrowserClass")
    instance = MagicMock(name="InstagramBrowserInstance")
    instance.page = MagicMock(name="ChromiumPage")
    mock_class.return_value = instance

    # Patch every import site we know about. Adding a new action handler
    # means adding its import path here too.
    mocker.patch("workers.core.executor.InstagramBrowser", mock_class)
    mocker.patch("workers.core.browser_core.ChromiumPage", autospec=False)
    mocker.patch(
        "workers.core.browser_core.create_proxy_extension",
        return_value="/tmp/fake-proxy-plugin",
    )
    return mock_class


# file fixtures
@pytest.fixture
def media_root(tmp_path: Path) -> str:
    """returns a fake media root."""
    root = tmp_path / "media"
    root.mkdir()
    return str(root)


@pytest.fixture
def safe_media_file(media_root: str) -> str:
    """creates a fake file for testing."""
    p = Path(media_root) / "video.mp4"
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42")  # plausible mp4 header
    return str(p)
