"""Task executor.

Takes a payload dict, starts an InstagramBrowser, injects cookies, runs
each command through its handler, and always closes the browser at the
end so we do not leave zombie Chrome processes around.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable, Dict, List

from workers.actions.action_update_profile import execute_update_profile
from workers.actions.action_upload import execute_upload
from workers.actions.action_warmup import execute_warmup
from workers.actions.action_gather_stats import execute_gather_stats
from workers.core.browser_core import InstagramBrowser
from workers.core.observability import (
    CheckpointException,
    MetricSample,
    ObservabilityMonitor,
)

logger = logging.getLogger(__name__)


# defaults
DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

DEFAULT_COOKIE_DOMAIN: str = ".instagram.com"
DEFAULT_COOKIE_PATH: str = "/"


# action registry
# handler signature: (browser: InstagramBrowser, args: dict) -> dict
ActionHandler = Callable[[InstagramBrowser, Dict[str, Any]], Dict[str, Any]]

ACTION_REGISTRY: Dict[str, ActionHandler] = {
    "warmup":         execute_warmup,
    # all upload kinds share one handler
    "upload_reels":   execute_upload,
    "upload_post":    execute_upload,
    "upload_story":   execute_upload,
    # profile and privacy edits
    "update_profile": execute_update_profile,
    # technical: headless stats collection
    "gather_stats":   execute_gather_stats,
}


# errors
class ExecutorError(RuntimeError):
    """Raised when the executor cannot start or dispatch a command."""


class UnknownActionError(ExecutorError):
    """Payload references an action with no handler registered."""


# executor
class TaskExecutor:
    """Owns the lifecycle of one browser session for one Task."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ExecutorError("payload must be a dict")

        self.payload: Dict[str, Any] = payload
        self.task_id: str | None = payload.get("task_id")
        self.account_id: str | None = payload.get("account_id")

        proxy_string = payload.get("proxy_string")
        allow_no_proxy = bool(payload.get("allow_no_proxy"))
        if not proxy_string and not allow_no_proxy:
            raise ExecutorError(
                f"payload is missing 'proxy_string' (task_id={self.task_id})"
            )
        # None -> InstagramBrowser runs with --no-proxy-server (direct).
        self.proxy_string: str | None = proxy_string or None

        self.user_agent: str = payload.get("user_agent") or DEFAULT_USER_AGENT
        self.headless: bool = bool(payload.get("headless", False))

        self.cookies: List[Dict[str, str]] = self._normalize_cookies(
            payload.get("cookies")
        )
        self.commands: List[Dict[str, Any]] = self._normalize_commands(
            payload.get("commands")
        )

    # public entrypoint
    def execute(self) -> Dict[str, Any]:
        """Run every command in order. Always close the browser at the end."""
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

        # make sure the browser is closed no matter what
        try:
            browser = InstagramBrowser(
                proxy_string=self.proxy_string,
                user_agent=self.user_agent,
                headless=self.headless,
                task_id=self.task_id,
            )

            # inject cookies if we have any
            if self.cookies:
                browser.inject_cookies(self.cookies)
            else:
                logger.info(
                    "[TaskExecutor] no cookies to inject for task_id=%s",
                    self.task_id,
                )

            # start the network and URL watchers before any command runs
            account_uuid = self._account_uuid()
            if account_uuid is not None:
                monitor = ObservabilityMonitor(
                    page=browser.page, account_id=account_uuid
                )
                monitor.start()
            else:
                logger.warning(
                    "[TaskExecutor] payload has no account_id, observability is off"
                )

            for index, command in enumerate(self.commands):
                # bail out if IG sent us to a challenge or suspended page
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
                try:
                    command_result = handler(browser, args) or {}
                except CheckpointException:
                    raise
                except Exception as handler_exc:
                    # On any action failure, snap the current page so the
                    # operator can see what was on screen. The artifacts
                    # land in /var/log/ig_crm/ (a docker volume in prod).
                    # We do NOT swallow the exception — re-raise after dump.
                    try:
                        from workers.core.browser_core import dump_page_artifacts
                        dump_page_artifacts(
                            browser.page,
                            reason=f"{action}_cmd{index}_{type(handler_exc).__name__}",
                        )
                    except Exception as dump_exc:
                        logger.warning(
                            "[TaskExecutor] artifact dump failed: %s", dump_exc
                        )
                    raise
                results.append(
                    {"index": index, "action": action, "result": command_result}
                )

            # one more check to catch a redirect that happened after the
            # last command
            if monitor is not None:
                monitor.check_checkpoint()

        finally:
            # stop the watchers before the page is gone
            if monitor is not None:
                try:
                    monitor.stop()
                    captured_samples = monitor.drain_samples()
                except Exception:
                    logger.exception(
                        "[TaskExecutor] observability stop/drain failed for task_id=%s",
                        self.task_id,
                    )

            # close the browser, clean folders
            if browser is not None:
                try:
                    browser.close()
                except Exception:
                    logger.exception(
                        "[TaskExecutor] browser.close() raised during shutdown, "
                        "task_id=%s. Going on.",
                        self.task_id,
                    )

        # save metrics and run analytics outside the browser try/finally
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
        """Save the captured metrics and run the shadowban check."""
        account_uuid = self._account_uuid()
        if account_uuid is None or not samples:
            return {"persisted": 0, "shadowban": None}

        # local imports so the module does not pull these at import time
        try:
            from app.core.database import SessionLocal
            from app.services import shadowban as shadowban_service
            from app.services.metrics import persist_samples
        except Exception:
            logger.exception(
                "[TaskExecutor] could not import metric/shadowban services, "
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

    # normalization helpers
    @staticmethod
    def _normalize_commands(raw: Any) -> List[Dict[str, Any]]:
        """Pull the commands list out of the payload."""
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
        """Turn the stored cookies blob into the list-of-dicts shape we use.

        Accepts:
            None or {}  -> []
            list[dict]  -> pass through
            dict[str, str] -> flat map, expanded with default IG domain and path.
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
            f"payload['cookies'] must be list, dict or None, got {type(raw).__name__}"
        )
