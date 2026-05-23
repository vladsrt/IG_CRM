"""Browser engine core.

Wraps DrissionPage Chromium instances with proxies and custom user agents.
Makes sure we do not leak temp extension folders if something crashes.
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


# constants
_PLUGIN_FOLDER_PREFIX: str = "runtime_proxy_plugin"
_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9_\-]")


def _sanitize_token(token: str) -> str:
    """Drop any char that is not safe in a filename (keep alnum, _ and -)."""
    return _SAFE_TOKEN_RE.sub("", token)


class InstagramBrowser:
    """Chromium browser set up for IG automation, with safe shutdown."""

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
        """Set up the browser.

        Args:
            proxy_string: Proxy URL (protocol://user:pass@host:port). Pass None to skip.
            user_agent: explicit User-Agent override.
            headless: run in headless mode.
            task_id: Task UUID, used to make the proxy extension folder name unique.
            account_user_agent: UA saved on the account in the db.
            account_platform: OS platform, used to pick a UA if nothing else is set.
            user_data_dir: path to a Chromium profile. "fresh" makes a new tempdir.

        Raises:
            ValueError: bad proxy format.
        """
        # turn empty string into None so the rest of the method has one
        # clear "no proxy" signal to check.
        self.proxy_string: Optional[str] = (
            proxy_string.strip() if isinstance(proxy_string, str) and proxy_string.strip() else None
        )
        self._uses_proxy: bool = self.proxy_string is not None
        # pick the UA up front. order: explicit > account-pinned > pool-by-
        # platform > Windows fallback. doing it here (not in the caller)
        # keeps the priority rule in one place and the action handler API
        # the same.
        self.user_agent = self._resolve_user_agent(
            explicit=user_agent,
            account_user_agent=account_user_agent,
            account_platform=account_platform,
        )
        self.task_id = task_id

        # init the cleanup attrs BEFORE any line that can raise. that way
        # self.close() is safe to call from the except branch even if the
        # constructor blew up half way through.
        self.page: Optional[ChromiumPage] = None
        self.co: Optional[ChromiumOptions] = None
        self.plugin_path: str = ""
        self.plugin_folder: str = ""
        # _owned_user_data_dir is the path we made for this instance,
        # close() removes it. _user_data_dir is the path we actually
        # pointed Chromium at (it can be caller-supplied, in that case
        # we leave it alone on close).
        self._owned_user_data_dir: str = ""
        self._user_data_dir: str = ""

        # pre-build a unique proxy folder path so close() can clean it later.
        if task_id:
            token = _sanitize_token(str(task_id))
            if not token:
                # task_id was non-empty but only had unsafe chars. fall back
                # to a random token so we never write to a shared dir.
                token = uuid.uuid4().hex
        else:
            token = uuid.uuid4().hex

        self.plugin_folder = f"{_PLUGIN_FOLDER_PREFIX}_{token}"

        try:
            # build ChromiumOptions and clear any cached proxy
            self.co = ChromiumOptions()

            # DrissionPage sometimes caches proxies in ~/.DrissionPage/.
            # we clear it here so it does not fight our setup.
            try:
                self.co.set_proxy("")  # type: ignore[arg-type]
            except Exception as exc:
                print(
                    f"[*] DP set_proxy('') not available ({exc}), relying on "
                    "the downstream guards"
                )

            # optional fresh / explicit user data dir.
            # in tests, "fresh" makes Chromium start with a clean profile
            # every launch: no cached proxy decisions, no old cookies, no
            # stale ServiceWorker state. in prod the caller can pin a
            # specific dir so cookies and UA stick across runs. mostly we
            # use the DP default, this is just a hook.
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
                    # last try: pass the flag directly
                    try:
                        self.co.set_argument(
                            f"--user-data-dir={self._user_data_dir}"
                        )
                        applied = True
                    except Exception as exc:
                        print(f"[!] could not pin user_data_dir via CLI flag: {exc}")
                if applied:
                    print(f"[*] Chromium user-data-dir set to {self._user_data_dir}")

            if self._uses_proxy:
                # build the MV3 extension for proxy auth
                generated = create_proxy_extension(
                    self.proxy_string, self.plugin_folder
                )
                if not generated:
                    raise RuntimeError(
                        "create_proxy_extension returned None even though "
                        "proxy_string was set, this is a bug"
                    )
                self.plugin_path = generated
                print(
                    f"[*] Proxy extension generated at {self.plugin_path} "
                    f"(proxy={self._redacted_proxy()})"
                )
                self.co.add_extension(self.plugin_path)
            else:
                # no proxy: hard-disable any cached proxy
                self.plugin_path = ""  # keep close() happy
                self._apply_no_proxy_settings(self.co)
                print("[*] No proxy set, going direct.")

            print(f"[*] Spoofing User-Agent: {self.user_agent}")

            self.co.set_user_agent(self.user_agent)

            # turn off images, pages load faster
            self.co.set_pref("profile.default_content_setting_values.images", 2)
            # block browser notifications
            self.co.set_pref("profile.default_content_setting_values.notifications", 2)

            # headless on/off
            self.co.headless(headless)

            # launch the Page. this is the heavy step. if it raises after
            # the Chrome subprocess is spawned, the except branch below
            # cleans up the orphan via self.close().
            self.page = ChromiumPage(self.co)
            print("[*] Browser launched.")
        except Exception:
            # constructor failed in the middle. clean up whatever made it
            # onto disk or into a subprocess before re-raising. the inner
            # try/except in close() makes sure a cleanup error does not
            # hide the real constructor error.
            try:
                self.close()
            except Exception as cleanup_exc:
                print(
                    f"[!] Cleanup during failed __init__ also raised: {cleanup_exc}"
                )
            raise

    # UA / proxy resolution helpers
    @staticmethod
    def _resolve_user_agent(
        *,
        explicit: Optional[str],
        account_user_agent: Optional[str],
        account_platform: Optional[str],
    ) -> str:
        """Pick a UA: explicit > db > random by platform."""
        # local import: keeps the workers/utils/ua_generator dep out of the
        # import path until we actually use it. the module has no side
        # effects so a top-level import would also be fine, this just keeps
        # cold-start time honest.
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
                # unknown platform string, fall through to the safe default.
                pass
        return get_random_user_agent(_Platform.WINDOWS)

    def _redacted_proxy(self) -> str:
        """Return the proxy URL with credentials hidden, safe to log."""
        if not self.proxy_string:
            return "(none)"
        if "@" not in self.proxy_string:
            return self.proxy_string
        # cut the credentials block: everything between "://" (or start) and "@".
        scheme_split = self.proxy_string.split("://", 1)
        if len(scheme_split) == 2:
            scheme, rest = scheme_split
            after_at = rest.split("@", 1)[1]
            return f"{scheme}://***@{after_at}"
        after_at = self.proxy_string.split("@", 1)[1]
        return f"***@{after_at}"

    @staticmethod
    def _apply_no_proxy_settings(co: ChromiumOptions) -> None:
        """Force a direct connection at every layer DrissionPage/Chromium has."""
        # 1. DrissionPage level clear, best effort.
        try:
            co.set_proxy("")  # type: ignore[arg-type]
        except Exception as exc:
            # some DrissionPage versions do not accept an empty string,
            # others do not expose set_proxy at all. the cli flag below is
            # the authoritative override anyway.
            print(f"[*] DrissionPage set_proxy('') not available ({exc}), using CLI flag")

        # 2. chromium command line flag, this one is authoritative.
        try:
            co.set_argument("--no-proxy-server")
        except Exception as exc:
            print(f"[!] Could not set --no-proxy-server flag: {exc}")

    @staticmethod
    def _sanitize_cookie(raw: Dict) -> Dict | None:
        """Normalize an exported cookie into something CDP Network.setCookie
        accepts. Browser-extension exports (Cookie-Editor etc.) carry fields CDP
        rejects: sameSite='no_restriction', expirationDate, hostOnly,
        firstPartyDomain, partitionKey, storeId. We keep only the valid subset.
        """
        name = raw.get("name")
        if not name:
            return None
        out: Dict = {
            "name": name,
            "value": raw.get("value") or "",
            "path": raw.get("path") or "/",
        }
        if raw.get("domain"):
            out["domain"] = raw["domain"]
        if "secure" in raw:
            out["secure"] = bool(raw["secure"])
        if "httpOnly" in raw:
            out["httpOnly"] = bool(raw["httpOnly"])
        # sameSite: CDP wants Strict/Lax/None; exports use no_restriction/lax/...
        ss = {"no_restriction": "None", "none": "None", "lax": "Lax",
              "strict": "Strict"}.get(str(raw.get("sameSite", "")).lower())
        if ss:
            out["sameSite"] = ss
            if ss == "None":
                out["secure"] = True  # SameSite=None requires Secure
        # expiry: accept expirationDate / expiry / expires
        exp = raw.get("expirationDate") or raw.get("expiry") or raw.get("expires")
        if exp:
            try:
                out["expires"] = float(exp)
            except (TypeError, ValueError):
                pass
        return out

    def inject_cookies(self, cookies_list: List[Dict[str, str]]) -> None:
        """Set cookies in the browser. We navigate to the target domain first.

        Each cookie is sanitized for CDP and injected independently, so one bad
        cookie can't abort the whole session.
        """
        print("[*] Navigating to robots.txt to set domain context...")
        self.page.get("https://www.instagram.com/robots.txt")
        time.sleep(2)

        print("[*] Injecting session cookies...")
        injected = 0
        for raw in cookies_list:
            clean = self._sanitize_cookie(raw)
            if clean is None:
                continue
            try:
                self.page.set.cookies(clean)
                injected += 1
            except Exception as e:
                print(f"[!] Skipped cookie {clean.get('name')!r}: {e}")

        print(f"[*] Injected {injected}/{len(cookies_list)} cookies.")
        time.sleep(2)

    def close(self) -> None:
        """Close the browser and clean up the plugin folder."""
        print("[*] Shutting down browser...")
        page = getattr(self, "page", None)
        if page is not None:
            try:
                page.quit()
            except Exception as e:
                print(f"[!] Error closing browser: {e}")
            finally:
                # drop the ref so a later close() call is cheap
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

        # only delete the user-data-dir if WE made it (user_data_dir="fresh").
        # caller-supplied paths stay where they are.
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

    # context manager
    # callers that use `with InstagramBrowser(...) as browser:` get clean
    # shutdown even on exceptions, which closes the last "user forgot to
    # call close()" gap that left stale runtime_proxy_plugin_* folders
    # piling up on disk.
    def __enter__(self) -> "InstagramBrowser":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.close()
        except Exception as cleanup_exc:
            # do not let cleanup hide the real exception that triggered
            # __exit__. log the cleanup failure and move on.
            print(f"[!] InstagramBrowser.__exit__ cleanup raised: {cleanup_exc}")

    # class-level janitor
    @staticmethod
    def cleanup_orphaned_extensions(
        root: str = ".",
        *,
        older_than_seconds: float = 0.0,
    ) -> int:
        """Delete runtime_proxy_plugin_* folders left behind by crashed runs.

        Args:
            root: directory to scan.
            older_than_seconds: only delete folders older than this.

        Returns:
            int: how many folders were removed.
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
