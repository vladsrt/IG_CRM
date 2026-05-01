"""
Task Executor
-------------
Consumes a payload dict (built by ``app.workers.celery_tasks.run_instagram_task``),
spins up an ``InstagramBrowser``, injects cookies, dispatches each command to
its registered handler, and *guarantees* browser teardown.

The strict requirement of Story 4.4 — no zombie Chrome processes, no
leaked proxy-plugin folders — is enforced by the ``try/finally`` block in
``execute()``.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Dict, List

from workers.actions.action_update_profile import execute_update_profile
from workers.actions.action_upload import execute_upload
from workers.actions.action_warmup import execute_warmup
from workers.core.browser_core import InstagramBrowser
from workers.core.observability import (
    CheckpointException,
    MetricSample,
    ObservabilityMonitor,
)

logger = logging.getLogger(__name__)


# ── Defaults ────────────────────────────────────────────────────────────
DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

DEFAULT_COOKIE_DOMAIN: str = ".instagram.com"
DEFAULT_COOKIE_PATH: str = "/"


# ── Action registry ─────────────────────────────────────────────────────
# Each handler signature: (browser: InstagramBrowser, args: dict) -> dict
ActionHandler = Callable[[InstagramBrowser, Dict[str, Any]], Dict[str, Any]]

ACTION_REGISTRY: Dict[str, ActionHandler] = {
    "warmup":         execute_warmup,
    # All three upload variants share one handler — IG decides server-side
    # whether a video becomes a Reel based on duration/aspect ratio.
    "upload_reels":   execute_upload,
    "upload_post":    execute_upload,
    "upload_story":   execute_upload,
    # Profile / privacy edits (Epic 9). One handler covers bio, avatar,
    # and the is_private toggle — the args dict drives which fields run.
    "update_profile": execute_update_profile,
    # Wire additional handlers here as they are implemented:
    # "send_dm":      execute_send_dm,
    # "like_post":    execute_like_post,
    # ...
}


# ── Errors ──────────────────────────────────────────────────────────────
class ExecutorError(RuntimeError):
    """Raised when the executor cannot start or dispatch a command."""


class UnknownActionError(ExecutorError):
    """The payload referenced an action that has no registered handler."""


# ── Executor ────────────────────────────────────────────────────────────
class TaskExecutor:
    """Owns the lifecycle of a single browser session for one Task."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ExecutorError("payload must be a dict")

        self.payload: Dict[str, Any] = payload
        self.task_id: str | None = payload.get("task_id")
        self.account_id: str | None = payload.get("account_id")

        proxy_string = payload.get("proxy_string")
        if not proxy_string:
            raise ExecutorError(
                f"payload is missing 'proxy_string' (task_id={self.task_id})"
            )
        self.proxy_string: str = proxy_string

        self.user_agent: str = payload.get("user_agent") or DEFAULT_USER_AGENT
        self.headless: bool = bool(payload.get("headless", False))

        self.cookies: List[Dict[str, str]] = self._normalize_cookies(
            payload.get("cookies")
        )
        self.commands: List[Dict[str, Any]] = self._normalize_commands(
            payload.get("commands")
        )

    # ── Public entrypoint ──────────────────────────────────────────────
    def execute(self) -> Dict[str, Any]:
        """Run every command in order. Always tears the browser down."""
        logger.info(
            "[TaskExecutor] starting task_id=%s account_id=%s commands=%d",
            self.task_id,
            self.account_id,
            len(self.commands),
        )

        if not self.commands:
            logger.warning(
                "[TaskExecutor] no commands to execute for task_id=%s", self.task_id
            )
            return {
                "task_id": self.task_id,
                "status": "noop",
                "executed": 0,
                "results": [],
            }

        browser: InstagramBrowser | None = None
        monitor: ObservabilityMonitor | None = None
        results: List[Dict[str, Any]] = []
        captured_samples: List[MetricSample] = []

        # ── Story 4.4: guaranteed teardown ─────────────────────────────
        try:
            browser = InstagramBrowser(
                proxy_string=self.proxy_string,
                user_agent=self.user_agent,
                headless=self.headless,
                task_id=self.task_id,
            )

            # Story 4.5: cookie injection
            if self.cookies:
                browser.inject_cookies(self.cookies)
            else:
                logger.info(
                    "[TaskExecutor] no cookies to inject for task_id=%s",
                    self.task_id,
                )

            # Epic 6: kick off network + URL watchers BEFORE any command runs.
            # Started here (not in __init__) so the browser/page exists.
            account_uuid = self._account_uuid()
            if account_uuid is not None:
                monitor = ObservabilityMonitor(
                    page=browser.page, account_id=account_uuid
                )
                monitor.start()
            else:
                logger.warning(
                    "[TaskExecutor] payload missing account_id — observability disabled"
                )

            for index, command in enumerate(self.commands):
                # Epic 6.2: between every command, abort if IG redirected
                # the session to a challenge / suspended page.
                if monitor is not None:
                    monitor.check_checkpoint()

                action = command.get("action")
                args = command.get("args") or {}
                if not isinstance(args, dict):
                    raise ExecutorError(
                        f"Command #{index} ({action!r}) has non-dict args: {type(args).__name__}"
                    )

                handler = ACTION_REGISTRY.get(action)
                if handler is None:
                    raise UnknownActionError(
                        f"Command #{index} references unknown action: {action!r}"
                    )

                logger.info(
                    "[TaskExecutor] dispatching command %d/%d action=%s",
                    index + 1,
                    len(self.commands),
                    action,
                )
                command_result = handler(browser, args) or {}
                results.append(
                    {"index": index, "action": action, "result": command_result}
                )

            # Final post-loop checkpoint sweep: a redirect after the last
            # command should still be surfaced as a CheckpointException.
            if monitor is not None:
                monitor.check_checkpoint()

        finally:
            # Stop the watchers + drain whatever they captured BEFORE the
            # browser teardown — once the page is gone, samples are gone too.
            if monitor is not None:
                try:
                    monitor.stop()
                    captured_samples = monitor.drain_samples()
                except Exception:
                    logger.exception(
                        "[TaskExecutor] observability stop/drain failed for task_id=%s",
                        self.task_id,
                    )

            # Crucial: zombie-process / proxy-folder cleanup.
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    logger.exception(
                        "[TaskExecutor] browser.close() raised during teardown — "
                        "task_id=%s. Continuing.",
                        self.task_id,
                    )

        # ── Persist metrics + run analytics OUTSIDE the browser try/finally
        # so a metric-write hiccup can never leak Chrome processes. ──────
        metrics_summary = self._persist_and_analyze(captured_samples)

        return {
            "task_id": self.task_id,
            "status": "ok",
            "executed": len(results),
            "results": results,
            "metrics": metrics_summary,
        }

    def _account_uuid(self) -> uuid.UUID | None:
        if not self.account_id:
            return None
        try:
            return uuid.UUID(self.account_id)
        except (ValueError, TypeError):
            return None

    def _persist_and_analyze(
        self, samples: List[MetricSample]
    ) -> Dict[str, Any]:
        """Flush captured metrics + invoke shadowban detector. Best-effort."""
        account_uuid = self._account_uuid()
        if account_uuid is None or not samples:
            return {"persisted": 0, "shadowban": None}

        # Local imports keep `workers.core` free of `app.*` import-time deps.
        try:
            from app.core.database import SessionLocal
            from app.services import shadowban as shadowban_service
            from app.services.metrics import persist_samples
        except Exception:
            logger.exception(
                "[TaskExecutor] could not import metric/shadowban services — "
                "skipping persistence for task_id=%s",
                self.task_id,
            )
            return {"persisted": 0, "shadowban": None}

        persisted = 0
        shadowban_result: Dict[str, Any] | None = None
        try:
            with SessionLocal() as db:
                persisted = persist_samples(db, account_uuid, samples)
                shadowban_result = shadowban_service.evaluate(db, account_uuid)
        except Exception:
            logger.exception(
                "[TaskExecutor] metric persist / shadowban analysis failed for "
                "task_id=%s",
                self.task_id,
            )

        return {"persisted": persisted, "shadowban": shadowban_result}

    # ── Normalization helpers ──────────────────────────────────────────
    @staticmethod
    def _normalize_commands(raw: Any) -> List[Dict[str, Any]]:
        """Accept either a list of commands or a full plan dict and flatten."""
        if raw is None:
            return []
        if isinstance(raw, list):
            return [c for c in raw if isinstance(c, dict)]
        if isinstance(raw, dict):
            inner = raw.get("commands")
            if isinstance(inner, list):
                return [c for c in inner if isinstance(c, dict)]
        raise ExecutorError(
            f"payload['commands'] must be a list or plan-dict, got {type(raw).__name__}"
        )

    @staticmethod
    def _normalize_cookies(raw: Any) -> List[Dict[str, str]]:
        """
        Coerce the stored cookie blob into the list-of-dicts shape that
        ``InstagramBrowser.inject_cookies`` expects.

        Accepts:
            * ``None`` or ``{}`` → no cookies
            * ``list[dict]``     → passed through (Instagram-export format)
            * ``dict[str, str]`` → treated as flat ``name → value`` map and
              expanded into a cookie list with the default IG domain/path
        """
        if not raw:
            return []
        if isinstance(raw, list):
            return [c for c in raw if isinstance(c, dict)]
        if isinstance(raw, dict):
            return [
                {
                    "name": str(name),
                    "value": str(value),
                    "domain": DEFAULT_COOKIE_DOMAIN,
                    "path": DEFAULT_COOKIE_PATH,
                }
                for name, value in raw.items()
            ]
        raise ExecutorError(
            f"payload['cookies'] must be list, dict, or None — got {type(raw).__name__}"
        )
