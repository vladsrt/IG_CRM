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
        user_agent: str,
        headless: bool = False,
        task_id: Optional[str] = None,
    ):
        """
        Initializes the browser environment.

        Args:
            proxy_string (str): Proxy string in IP:PORT or USER:PASS@IP:PORT format.
            user_agent (str): User-Agent string to spoof.
            headless (bool): Whether to run the browser in headless mode.
            task_id (str | None): Optional Task UUID used to derive a unique
                proxy-extension folder name. If omitted, a random hex token
                is generated. This is what makes concurrent ``InstagramBrowser``
                instances in the same process safe.
        """
        self.proxy_string = proxy_string
        self.user_agent = user_agent
        self.task_id = task_id

        # 1. Compute a unique, filesystem-safe folder name for this instance.
        if task_id:
            token = _sanitize_token(str(task_id))
            if not token:
                # task_id was non-empty but consisted entirely of unsafe chars —
                # fall back to a random token so we never write to a shared dir.
                token = uuid.uuid4().hex
        else:
            token = uuid.uuid4().hex

        self.plugin_folder: str = f"{_PLUGIN_FOLDER_PREFIX}_{token}"

        # 2. Generate Proxy Extension into the unique folder.
        self.plugin_path: str = create_proxy_extension(
            self.proxy_string, self.plugin_folder
        )
        print(f"[*] Proxy extension generated at {self.plugin_path}")

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

        # 4. Launch Page
        self.page = ChromiumPage(self.co)
        print("[*] Browser launched successfully.")

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
        proxy-extension folder. Crucial for preventing memory leaks and
        orphaned proxy folders — and, with the unique folder name, also
        crucial for not nuking a sibling browser's plugin in concurrent runs.
        """
        print("[*] Shutting down browser...")
        try:
            if hasattr(self, "page") and self.page:
                self.page.quit()
        except Exception as e:
            print(f"[!] Error closing browser: {e}")

        print(f"[*] Cleaning up proxy extension directory: {self.plugin_path}")
        try:
            if self.plugin_path and os.path.exists(self.plugin_path):
                shutil.rmtree(self.plugin_path)
                print(f"[*] Removed {self.plugin_path}")
        except Exception as e:
            print(f"[!] Error cleaning proxy directory: {e}")
