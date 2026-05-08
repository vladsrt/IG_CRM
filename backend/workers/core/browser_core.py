"""
Core Browser Engine
-------------------
Handles the instantiation of DrissionPage Chromium instances with proxies,
custom user agents, and performance tweaks. Manages session injection and
clean teardown.

Concurrency note
~~~~~~~~~~~~~~~~
Each ``InstagramBrowser`` instance writes its proxy-auth Chrome extension
into a *unique* folder, so multiple tasks running in parallel inside the
same Celery worker process never collide on disk. The folder name is
derived from the optional ``task_id`` argument when available, otherwise
from a fresh ``uuid.uuid4().hex``.

Resource-leak invariant (CRITICAL-3)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``__init__`` is wrapped in a try/except that calls ``self.close()`` on
ANY partial failure before re-raising. Cleanup-relevant attributes
(``self.page``, ``self.plugin_path``) are initialized to safe defaults
BEFORE the first line that can raise, so ``self.close()`` is callable on
a partially-constructed instance without ``AttributeError``.
"""

import os
import re
import shutil
import time
import uuid
from typing import Dict, List, Optional

from DrissionPage import ChromiumPage, ChromiumOptions
from workers.utils.proxy_builder import create_proxy_extension


# ── Module-level constants ──────────────────────────────────────────────
_PLUGIN_FOLDER_PREFIX: str = "runtime_proxy_plugin"
_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9_\-]")


def _sanitize_token(token: str) -> str:
    """Strip any character that isn't filesystem-safe (alnum / `_` / `-`)."""
    return _SAFE_TOKEN_RE.sub("", token)


class InstagramBrowser:
    """
    Manages a Chromium browser instance customized for Instagram automation.
    Handles proxy extension generation and safe teardown to prevent leaks.
    """

    def __init__(
        self,
        proxy_string: str,
        user_agent: Optional[str] = None,
        headless: bool = False,
        task_id: Optional[str] = None,
        *,
        account_user_agent: Optional[str] = None,
        account_platform: Optional[str] = None,
    ):
        """
        Initializes the browser environment.

        Args:
            proxy_string: Proxy URL in any form accepted by
                :func:`workers.utils.proxy_builder.parse_proxy_url`.
                Preferred form is ``protocol://user:pass@host:port``;
                a bare ``user:pass@host:port`` is treated as ``http``.
            user_agent: Explicit User-Agent override. If omitted, the
                engine resolves a UA in this priority order:

                    1. ``account_user_agent`` (the value persisted on
                       the ``InstagramAccount`` row — preferred path,
                       since each account is supposed to keep one UA
                       for life).
                    2. A random Chrome UA from the pool that matches
                       ``account_platform`` (last-resort fallback for
                       legacy rows where the column is still NULL).
                    3. A safe Windows Chrome UA — only if neither of
                       the above is available.

            headless: Whether to run the browser in headless mode.
            task_id: Optional Task UUID used to derive a unique
                proxy-extension folder name. If omitted, a random hex
                token is generated. This is what makes concurrent
                ``InstagramBrowser`` instances in the same process safe.
            account_user_agent: The ``user_agent`` value persisted on
                the ``InstagramAccount`` row. Pass this when launching a
                browser for a specific account so the spoofed
                fingerprint stays pinned across sessions.
            account_platform: The account's ``platform`` enum value
                (``windows`` / ``macos`` / ``linux``). Used only when
                ``account_user_agent`` is missing — to seed a UA from
                the matching pool.

        Raises:
            ValueError: if ``proxy_string`` cannot be parsed.
            Any exception raised during Chromium setup is re-raised
            AFTER ``self.close()`` has reaped whatever was partially
            constructed (Chrome subprocess, plugin folder on disk).
        """
        self.proxy_string = proxy_string
        # Resolve the UA up front — explicit > account-pinned > pool-by-platform
        # > Windows fallback. Doing this here (rather than in the caller) keeps
        # the priority rule in one place and the public API of every action
        # handler unchanged.
        self.user_agent = self._resolve_user_agent(
            explicit=user_agent,
            account_user_agent=account_user_agent,
            account_platform=account_platform,
        )
        self.task_id = task_id

        # CRITICAL-3 — initialize cleanup-relevant attributes BEFORE any
        # line that can raise, so self.close() is safe to call from the
        # except branch even if construction blew up halfway through.
        self.page: Optional[ChromiumPage] = None
        self.co: Optional[ChromiumOptions] = None
        self.plugin_path: str = ""
        self.plugin_folder: str = ""

        # 1. Compute a unique, filesystem-safe folder name for this instance.
        if task_id:
            token = _sanitize_token(str(task_id))
            if not token:
                # task_id was non-empty but consisted entirely of unsafe chars —
                # fall back to a random token so we never write to a shared dir.
                token = uuid.uuid4().hex
        else:
            token = uuid.uuid4().hex

        self.plugin_folder = f"{_PLUGIN_FOLDER_PREFIX}_{token}"

        try:
            # 2. Generate Proxy Extension into the unique folder. The
            # builder accepts a full ``protocol://user:pass@host:port``
            # URL so HTTP/HTTPS/SOCKS proxies all share one path.
            self.plugin_path = create_proxy_extension(
                self.proxy_string, self.plugin_folder
            )
            print(
                f"[*] Proxy extension generated at {self.plugin_path} "
                f"(proxy={self._redacted_proxy()})"
            )
            print(f"[*] Spoofing User-Agent: {self.user_agent}")

            # 3. Setup Options
            self.co = ChromiumOptions()
            self.co.add_extension(self.plugin_path)
            self.co.set_user_agent(self.user_agent)

            # Disable images to speed up page loading
            self.co.set_pref("profile.default_content_setting_values.images", 2)
            # Block browser notifications
            self.co.set_pref("profile.default_content_setting_values.notifications", 2)

            # Set headless preference
            self.co.headless(headless)

            # 4. Launch Page — this is the big-cost step. If it raises after
            # spawning the Chrome subprocess, the except branch below reaps
            # the orphan via self.close().
            self.page = ChromiumPage(self.co)
            print("[*] Browser launched successfully.")
        except Exception:
            # Partial construction failed. Reap whatever made it onto disk
            # or into a subprocess before re-raising. The nested try/except
            # inside close() ensures a cleanup error cannot mask the
            # original construction error.
            try:
                self.close()
            except Exception as cleanup_exc:
                print(
                    f"[!] Cleanup during failed __init__ also raised: {cleanup_exc}"
                )
            raise

    # ── UA / proxy resolution helpers ───────────────────────────────────
    @staticmethod
    def _resolve_user_agent(
        *,
        explicit: Optional[str],
        account_user_agent: Optional[str],
        account_platform: Optional[str],
    ) -> str:
        """Pick the UA to spoof, applying the priority rule documented above.

        Kept as a static method so it can be unit-tested without booting a
        Chromium subprocess.
        """
        # Local import — keeps the workers/utils/ua_generator dependency
        # out of the import path until we actually need it. The module
        # itself has no side effects so a top-level import would also be
        # fine; this just keeps cold-start time honest.
        from workers.utils.ua_generator import (
            Platform as _Platform,
            get_random_user_agent,
        )

        if explicit:
            return explicit
        if account_user_agent:
            return account_user_agent
        if account_platform:
            try:
                return get_random_user_agent(account_platform)
            except ValueError:
                # Unknown platform string — fall through to the safe default.
                pass
        return get_random_user_agent(_Platform.WINDOWS)

    def _redacted_proxy(self) -> str:
        """Return the proxy URL with credentials masked, for safe logging."""
        if "@" not in self.proxy_string:
            return self.proxy_string
        # Strip the credential block (everything between "://"-or-start and "@").
        scheme_split = self.proxy_string.split("://", 1)
        if len(scheme_split) == 2:
            scheme, rest = scheme_split
            after_at = rest.split("@", 1)[1]
            return f"{scheme}://***@{after_at}"
        after_at = self.proxy_string.split("@", 1)[1]
        return f"***@{after_at}"

    def inject_cookies(self, cookies_list: List[Dict[str, str]]) -> None:
        """
        Injects a list of cookies into the browser context.
        Navigates to robots.txt first to ensure the domain context is correct.

        Args:
            cookies_list: A list of cookie dictionaries.
        """
        print("[*] Navigating to robots.txt to set domain context...")
        self.page.get("https://www.instagram.com/robots.txt")
        time.sleep(2)

        print("[*] Injecting session cookies...")
        for cookie in cookies_list:
            self.page.set.cookies(cookie)

        print(f"[*] Injected {len(cookies_list)} cookies.")
        time.sleep(2)

    def close(self) -> None:
        """
        Safely shuts down the browser and cleans up *this instance's*
        proxy-extension folder. Idempotent and tolerant of partial
        construction — uses ``getattr`` defaults so it works even when
        ``__init__`` raised before any attribute was set.
        """
        print("[*] Shutting down browser...")
        page = getattr(self, "page", None)
        if page is not None:
            try:
                page.quit()
            except Exception as e:
                print(f"[!] Error closing browser: {e}")
            finally:
                # Drop the ref so a follow-up close() call is cheap.
                self.page = None

        plugin_path = getattr(self, "plugin_path", "")
        if plugin_path:
            print(f"[*] Cleaning up proxy extension directory: {plugin_path}")
            try:
                if os.path.exists(plugin_path):
                    shutil.rmtree(plugin_path)
                    print(f"[*] Removed {plugin_path}")
            except Exception as e:
                print(f"[!] Error cleaning proxy directory: {e}")
            finally:
                self.plugin_path = ""
