"""Shared fixtures for the IG CRM test suite.

Design notes
~~~~~~~~~~~~
* Tests are unit-level: no real Postgres, no real Chromium, no real Redis,
  no real OpenAI. Every external boundary is mocked at the import seam of
  the module under test.
* The FastAPI ``TestClient`` is wired to the real app, but ``get_db`` is
  overridden to yield a no-op MagicMock — every test that touches CRUD
  patches the CRUD module directly, bypassing the session entirely.
* For worker tests, ``SessionLocal`` is patched to return a context-manager
  MagicMock built by :func:`make_mock_session` so we can stage the
  ``db.get`` / ``db.execute`` returns per-test.

Required dev dependencies::

    pip install pytest pytest-mock httpx
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


# ── sys.path bootstrap ──────────────────────────────────────────────────
# Make ``app.*`` and ``workers.*`` importable when pytest is invoked from
# any directory. backend/ is the parent of this tests/ folder.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))


# ── Environment variables (must be set BEFORE app imports) ──────────────
# These prevent app.core.config.Settings from blowing up on first import,
# and stop services like AIParser from trying to call out to OpenAI.
os.environ.setdefault("OPENAI_API_KEY", "sk-test-suite")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")
os.environ.setdefault("MEDIA_ROOT", "/tmp/ig-crm-test-media")


# ── Factory helpers (importable from any test file) ─────────────────────
def make_fake_account(**overrides: Any) -> SimpleNamespace:
    """Return a ``SimpleNamespace`` shaped like an ``InstagramAccount`` row.

    SimpleNamespace works with Pydantic v2's ``from_attributes=True`` so
    it can be returned through a FastAPI route's ``response_model``.
    """
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
    """Build a context-manager MagicMock that imitates ``SessionLocal()``.

    Returns ``(context_manager, inner_session)``. Patch
    ``app.workers.celery_tasks.SessionLocal`` (or wherever it's imported)
    with ``return_value=context_manager``; assert against ``inner_session``.

    The ``execute_results`` list is consumed in order — one entry per
    expected ``db.execute(...)`` call. Each entry should be a MagicMock
    whose ``.scalar_one_or_none()`` returns the desired value.
    """
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
    """A :class:`TrustReport` that satisfies the fan-out trust gate."""
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
    """A :class:`TrustReport` that fails the fan-out trust gate."""
    from app.services.trust import TrustReport
    return TrustReport(
        score=15,
        proxy_ok=False,
        proxy_latency_ms=None,
        user_agent_ok=True,
        hygiene_ok=False,
        reasons=[reason],
    )


# ── FastAPI TestClient with mocked get_db ───────────────────────────────
@pytest.fixture
def client():
    """FastAPI ``TestClient`` with ``get_db`` overridden to yield a no-op mock.

    Every test that touches DB-bound routes mocks the CRUD layer
    explicitly via ``mocker.patch(...)``. The overridden ``get_db`` is
    just there so dependency resolution succeeds.
    """
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


# ── Mocked browser (DrissionPage neutralized at the seam) ───────────────
@pytest.fixture
def mocked_browser_class(mocker):
    """Replace ``InstagramBrowser`` everywhere it's imported by handlers + executor.

    Returns the mock class. ``.return_value`` is the per-instance mock —
    set ``.page``, ``.inject_cookies``, etc. on it to drive handler code.
    """
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


# ── Filesystem fixtures ─────────────────────────────────────────────────
@pytest.fixture
def media_root(tmp_path: Path) -> str:
    """Per-test MEDIA_ROOT directory. Yielded as a ``str`` to match settings."""
    root = tmp_path / "media"
    root.mkdir()
    return str(root)


@pytest.fixture
def safe_media_file(media_root: str) -> str:
    """Create a real file inside ``media_root`` and return its absolute path."""
    p = Path(media_root) / "video.mp4"
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42")  # plausible mp4 header
    return str(p)
