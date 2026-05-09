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
import tempfile
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
        proxy_string: Optional[str],
        user_agent: Optional[str] = None,
        headless: bool = False,
        task_id: Optional[str] = None,
        *,
        account_user_agent: Optional[str] = None,
        account_platform: Optional[str] = None,
        user_data_dir: Optional[str] = None,
    ):
        """
        Initializes the browser environment.

        Args:
            proxy_string: Proxy URL in any form accepted by
                :func:`workers.utils.proxy_builder.parse_proxy_url`.
                Preferred form is ``protocol://user:pass@host:port``;
                a bare ``user:pass@host:port`` is treated as ``http``.
                Pass ``None`` or an empty string to launch with NO
                proxy — the extension generation is fully bypassed
                in that case AND ``--no-proxy-server`` is added to
                Chromium's command line so DrissionPage can't bring
                a stale proxy config back from its on-disk config
                cache.
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
            user_data_dir: Path to a Chromium profile directory.

                * ``None`` (default) — let DrissionPage pick (its
                  config-managed default).
                * ``"fresh"`` — create a brand-new tempdir for this
                  run. ``close()`` will delete it on teardown. Use
                  this in test environments to guarantee a clean
                  Chrome profile (no cached proxy decisions, no
                  leftover cookies, no stale ServiceWorker state).
                * Any other path — use that directory as the profile
                  dir. Caller owns its lifecycle.

        Raises:
            ValueError: if ``proxy_string`` is non-empty and cannot
                be parsed.
            Any exception raised during Chromium setup is re-raised
            AFTER ``self.close()`` has reaped whatever was partially
            constructed (Chrome subprocess, plugin folder on disk).
        """
        # Normalize empty string → None so the rest of the method has a
        # single "no proxy" signal to branch on.
        self.proxy_string: Optional[str] = (
            proxy_string.strip() if isinstance(proxy_string, str) and proxy_string.strip() else None
        )
        self._uses_proxy: bool = self.proxy_string is not None
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
        # ``_owned_user_data_dir`` is the path we created for this
        # instance — close() deletes it. ``_user_data_dir`` is the path
        # we ACTUALLY pointed Chromium at (could be caller-supplied,
        # in which case we don't delete on close).
        self._owned_user_data_dir: str = ""
        self._user_data_dir: str = ""

        # 1. Compute a unique, filesystem-safe folder name for this
        # instance — used only on the with-proxy branch, but computed
        # eagerly so close() can reference it consistently.
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
            # 2. ChromiumOptions setup. The proxy extension generation
            # and `--no-proxy-server` paths are mutually exclusive — we
            # NEVER do both, NEVER do neither.
            self.co = ChromiumOptions()

            # ── Always clear DrissionPage's INI-cached proxy. ─────────
            # Whether or not WE configure a proxy this run, DP's
            # per-user config (under ``~/.DrissionPage/``) may still
            # carry a stale ``--proxy-server=`` from a previous run.
            # That stale flag races our proxy extension at launch
            # (Chrome applies CLI flags before extensions load) and
            # produces ``ERR_TUNNEL_CONNECTION_FAILED`` /
            # ``ERR_PROXY_CONNECTION_FAILED`` on the first navigation.
            # Clearing here is belt-and-braces: the no-proxy branch
            # below also pins ``--no-proxy-server``; the with-proxy
            # branch lets the extension's chrome.proxy.settings.set
            # be the only proxy config in play.
            try:
                self.co.set_proxy("")  # type: ignore[arg-type]
            except Exception as exc:
                print(
                    f"[*] DP set_proxy('') unavailable ({exc}); relying on "
                    "downstream guards"
                )

            # ── Optional fresh / explicit user data dir. ──────────────
            # In test environments, "fresh" guarantees Chromium starts
            # against a clean profile every launch — no cached proxy
            # decisions, no leftover cookies, no stale ServiceWorker
            # state. In prod, the caller can pin a specific dir so
            # cookies + UA persist across runs (mostly we use the DP
            # default; this is a hook).
            if user_data_dir == "fresh":
                self._owned_user_data_dir = tempfile.mkdtemp(
                    prefix="ig_crm_chrome_profile_"
                )
                self._user_data_dir = self._owned_user_data_dir
            elif user_data_dir:
                self._user_data_dir = os.path.abspath(user_data_dir)
            if self._user_data_dir:
                applied = False
                for setter in ("set_user_data_path", "set_paths"):
                    fn = getattr(self.co, setter, None)
                    if not callable(fn):
                        continue
                    try:
                        if setter == "set_paths":
                            fn(user_data_path=self._user_data_dir)
                        else:
                            fn(self._user_data_dir)
                        applied = True
                        break
                    except Exception as exc:
                        print(
                            f"[*] DP {setter}({self._user_data_dir!r}) failed ({exc})"
                        )
                if not applied:
                    # Last-resort: pass the flag directly.
                    try:
                        self.co.set_argument(
                            f"--user-data-dir={self._user_data_dir}"
                        )
                        applied = True
                    except Exception as exc:
                        print(f"[!] could not pin user_data_dir via CLI flag: {exc}")
                if applied:
                    print(f"[*] Chromium user-data-dir → {self._user_data_dir}")

            if self._uses_proxy:
                # ── With-proxy branch ─────────────────────────────────
                # Generate the MV3 proxy extension into the unique
                # folder. The builder accepts a full
                # ``protocol://user:pass@host:port`` URL so
                # HTTP/HTTPS/SOCKS proxies all share one path. It
                # returns the absolute path to the generated folder,
                # or ``None`` if it decided not to generate one
                # (which only happens on a falsy proxy_string — we
                # guard against that above, but check defensively).
                generated = create_proxy_extension(
                    self.proxy_string, self.plugin_folder
                )
                if not generated:
                    raise RuntimeError(
                        "create_proxy_extension returned None despite a "
                        "non-empty proxy_string — this is a bug"
                    )
                self.plugin_path = generated
                print(
                    f"[*] Proxy extension generated at {self.plugin_path} "
                    f"(proxy={self._redacted_proxy()})"
                )
                self.co.add_extension(self.plugin_path)
            else:
                # ── No-proxy branch ───────────────────────────────────
                # DrissionPage retains a per-user config (under
                # ``~/.DrissionPage/`` / its INI file) that can keep a
                # proxy from a previous run. The two safest belt-and-
                # braces guards:
                #
                #   1. Don't load any proxy extension (this whole
                #      block was skipped).
                #   2. Pass ``--no-proxy-server`` on Chromium's
                #      command line — this overrides any stale
                #      ``--proxy-server=`` flag the saved config
                #      might inject.
                #
                # We also call ``set_proxy("")`` if the API exists on
                # this DrissionPage version, which clears the config-
                # level proxy in addition to the command line.
                self.plugin_path = ""  # explicitly empty for close()
                self._apply_no_proxy_settings(self.co)
                print("[*] No proxy configured — running on direct connection.")

            print(f"[*] Spoofing User-Agent: {self.user_agent}")

            self.co.set_user_agent(self.user_agent)

            # Disable images to speed up page loading.
            self.co.set_pref("profile.default_content_setting_values.images", 2)
            # Block browser notifications.
            self.co.set_pref("profile.default_content_setting_values.notifications", 2)

            # Set headless preference.
            self.co.headless(headless)

            # 3. Launch Page — this is the big-cost step. If it raises
            # after spawning the Chrome subprocess, the except branch
            # below reaps the orphan via self.close().
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
        if not self.proxy_string:
            return "(none)"
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

    @staticmethod
    def _apply_no_proxy_settings(co: ChromiumOptions) -> None:
        """Force a direct connection at every layer DrissionPage / Chromium expose.

        DrissionPage stores its last-used proxy in a per-user INI
        (under ``~/.DrissionPage/``); without an explicit override the
        next run can pick that stale config back up — which is exactly
        what produced the "browser fails to connect when no proxy is
        provided" regression. We:

        1. Clear the config-level proxy via ``set_proxy("")`` if the
           method exists on this DrissionPage version (best-effort
           — different DP versions expose different signatures).
        2. Force ``--no-proxy-server`` on Chromium's command line.
           This is the only override that survives a stale INI: a
           command-line flag wins over both the config file and any
           ``--proxy-server=`` that DrissionPage might inject.
        """
        # 1. DrissionPage-level clear (best effort).
        try:
            co.set_proxy("")  # type: ignore[arg-type]
        except Exception as exc:
            # Some DrissionPage versions don't accept an empty string,
            # others don't expose set_proxy at all. The command-line
            # flag below is the authoritative override regardless.
            print(f"[*] DrissionPage set_proxy('') unavailable ({exc}); relying on CLI flag")

        # 2. Chromium command-line flag — authoritative.
        try:
            co.set_argument("--no-proxy-server")
        except Exception as exc:
            print(f"[!] Could not set --no-proxy-server flag: {exc}")

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

        # Remove the user-data-dir ONLY if we created it ourselves
        # (user_data_dir="fresh"). Caller-supplied paths are left alone.
        owned = getattr(self, "_owned_user_data_dir", "")
        if owned:
            print(f"[*] Cleaning up Chromium profile directory: {owned}")
            try:
                if os.path.exists(owned):
                    shutil.rmtree(owned, ignore_errors=True)
            except Exception as e:
                print(f"[!] Error cleaning profile directory: {e}")
            finally:
                self._owned_user_data_dir = ""
                self._user_data_dir = ""

    # ── Context-manager protocol ────────────────────────────────────────
    # Callers that use ``with InstagramBrowser(...) as browser:`` get
    # guaranteed cleanup even on exceptions, which closes the last
    # remaining "operator forgot to call close()" hole that lets stale
    # ``runtime_proxy_plugin_*`` folders accumulate on disk.
    def __enter__(self) -> "InstagramBrowser":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.close()
        except Exception as cleanup_exc:
            # Never let cleanup mask the real exception that triggered
            # __exit__. Log the cleanup failure and carry on.
            print(f"[!] InstagramBrowser.__exit__ cleanup raised: {cleanup_exc}")

    # ── Class-level janitorial sweep ────────────────────────────────────
    @staticmethod
    def cleanup_orphaned_extensions(
        root: str = ".",
        *,
        older_than_seconds: float = 0.0,
    ) -> int:
        """Remove ``runtime_proxy_plugin_*`` directories left behind by
        crashed sessions.

        Production code already cleans up its own extension folder via
        :meth:`close`, so this method is purely a janitorial safety
        net for the case where the worker process was SIGKILLed mid-
        flight (Celery shutdown, OOM, host reboot, etc.) and never
        ran ``close()``. Safe to invoke from a Celery beat task or a
        worker startup hook.

        Args:
            root: Directory to scan. Defaults to the current working
                dir, which is where ``create_proxy_extension`` writes
                its folders.
            older_than_seconds: Only remove folders whose mtime is at
                least this many seconds in the past. Defaults to 0
                (every matching folder). Set this to a sane value
                (e.g. 3600) when running concurrently with live
                sessions, so the sweep can't race a folder that was
                created two seconds ago by another worker.

        Returns:
            Number of folders actually removed.
        """
        import time as _time
        removed = 0
        try:
            entries = os.listdir(root)
        except FileNotFoundError:
            return 0
        cutoff = _time.time() - max(0.0, older_than_seconds)
        for name in entries:
            if not name.startswith(_PLUGIN_FOLDER_PREFIX + "_"):
                continue
            path = os.path.join(root, name)
            if not os.path.isdir(path):
                continue
            try:
                if older_than_seconds > 0 and os.path.getmtime(path) > cutoff:
                    continue
                shutil.rmtree(path)
                removed += 1
                print(f"[*] cleanup_orphaned_extensions: removed {path}")
            except Exception as exc:
                print(
                    f"[!] cleanup_orphaned_extensions: could not remove {path}: {exc}"
                )
        return removed
