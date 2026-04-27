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
from typing import Any, Callable, Dict, List

from workers.actions.action_upload import execute_upload
from workers.actions.action_warmup import execute_warmup
from workers.core.browser_core import InstagramBrowser

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
    "warmup":       execute_warmup,
    # All three upload variants share one handler — IG decides server-side
    # whether a video becomes a Reel based on duration/aspect ratio.
    "upload_reels": execute_upload,
    "upload_post":  execute_upload,
    "upload_story": execute_upload,
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
        results: List[Dict[str, Any]] = []

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

            for index, command in enumerate(self.commands):
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

        finally:
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

        return {
            "task_id": self.task_id,
            "status": "ok",
            "executed": len(results),
            "results": results,
        }

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
