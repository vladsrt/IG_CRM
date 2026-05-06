"""
Local Action Test Harness
-------------------------
Stand-alone driver for every DrissionPage action in this project.
Bypasses Celery, the orchestrator, the database, and even the proxy
extension if you don't pass one — so you can iterate on action code in
isolation without spinning up the full stack.

Usage
~~~~~
    1. Paste your IG cookies into ``COOKIES`` below.
    2. Optionally set ``PROXY`` to a "user:pass@host:port" string. Leave
       it as ``None`` to launch Chromium with no proxy at all (good for
       LAN-only testing on a residential IP).
    3. Scroll to the bottom of this file and uncomment ONE of the
       ``test_*()`` calls in ``__main__``. Run:

           python test_actions_local.py

       or, if you prefer per-test CLI dispatch:

           python test_actions_local.py warmup
           python test_actions_local.py update_profile --bio "hello"
           python test_actions_local.py upload --file /path/to.mp4

What it gives you that the existing ``backend/test_local.py`` does not
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
* ``InstagramBrowser`` is used when a proxy IS provided (real prod path
  — proxy extension, fingerprint flags, the works).
* When no proxy is provided we fall back to a minimal ``ChromiumPage``
  setup so you can test on your home connection without proxy auth.
* Covers every action: warmup, update_profile, upload.
* Each ``test_*`` returns the action's result dict so you can inspect
  it from a Python REPL too.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional

# Make `workers.*` and `app.*` importable when this file lives at repo root.
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.join(_HERE, "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from DrissionPage import ChromiumOptions, ChromiumPage  # noqa: E402

from workers.actions.action_update_profile import execute_update_profile  # noqa: E402
from workers.actions.action_upload import execute_upload  # noqa: E402
from workers.actions.action_warmup import execute_warmup  # noqa: E402
from workers.core.browser_core import InstagramBrowser  # noqa: E402

logger = logging.getLogger(__name__)


# ─── Edit these ────────────────────────────────────────────────────────
PROXY: Optional[str] = None  # e.g. "user:pass@gw.dataimpulse.com:823"

USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

COOKIES: List[Dict[str, str]] = [
    # Drop your real cookies here — same shape that
    # InstagramBrowser.inject_cookies / page.set.cookies expects.
    # Example (replace values):
    # {"domain": ".instagram.com", "name": "sessionid",  "value": "...", "path": "/"},
    # {"domain": ".instagram.com", "name": "ds_user_id", "value": "...", "path": "/"},
    # {"domain": ".instagram.com", "name": "csrftoken",  "value": "...", "path": "/"},
    # {"domain": ".instagram.com", "name": "datr",       "value": "...", "path": "/"},
    # {"domain": ".instagram.com", "name": "mid",        "value": "...", "path": "/"},
    # {"domain": ".instagram.com", "name": "rur",        "value": "...", "path": "/"},
]

HEADLESS: bool = False  # flip to True for CI-style runs


# ─── Browser facade ────────────────────────────────────────────────────
class _BrowserFacade:
    """Duck-types ``InstagramBrowser`` for the no-proxy code path.

    Action handlers only ever read ``browser.page`` — so we only need
    to expose that one attribute. Production still uses the real
    ``InstagramBrowser`` whenever ``PROXY`` is set.
    """

    def __init__(self, page: ChromiumPage) -> None:
        self.page = page

    def inject_cookies(self, cookies_list: List[Dict[str, str]]) -> None:
        self.page.get("https://www.instagram.com/robots.txt")
        time.sleep(1.5)
        for cookie in cookies_list:
            self.page.set.cookies(cookie)
        time.sleep(1.0)

    def close(self) -> None:
        try:
            self.page.quit()
        except Exception as exc:
            logger.debug("[harness] page.quit() failed (%s); ignoring", exc)


def _open_browser() -> Any:
    """Return a browser-like object with a ``.page`` attribute and cookies loaded."""
    if PROXY:
        logger.info("[harness] launching real InstagramBrowser via proxy")
        browser = InstagramBrowser(
            proxy_string=PROXY,
            user_agent=USER_AGENT,
            headless=HEADLESS,
        )
        browser.inject_cookies(COOKIES)
        return browser

    logger.info("[harness] no proxy set — launching plain ChromiumPage")
    co = ChromiumOptions()
    co.set_user_agent(USER_AGENT)
    co.set_pref("profile.default_content_setting_values.notifications", 2)
    co.headless(HEADLESS)
    page = ChromiumPage(co)

    facade = _BrowserFacade(page)
    facade.inject_cookies(COOKIES)
    return facade


# ─── Test functions ────────────────────────────────────────────────────
def test_warmup(**override: Any) -> Dict[str, Any]:
    """Run a Warmup 2.0 session. Pass any ``execute_warmup`` arg to override."""
    browser = _open_browser()
    try:
        defaults: Dict[str, Any] = {
            "total_minutes_min": 1.0,
            "total_minutes_max": 2.0,
            "phases_min": 2,
            "phases_max": 4,
        }
        defaults.update(override)
        result = execute_warmup(browser, args=defaults)
        print(f"[+] warmup result: {result}")
        return result
    finally:
        browser.close()


def test_update_profile(
    bio: Optional[str] = None,
    avatar_path: Optional[str] = None,
    is_private: Optional[bool] = None,
) -> Dict[str, Any]:
    """Run the update-profile action with the supplied fields."""
    browser = _open_browser()
    try:
        args: Dict[str, Any] = {}
        if bio is not None:
            args["bio"] = bio
        if avatar_path is not None:
            args["avatar_path"] = avatar_path
        if is_private is not None:
            args["is_private"] = is_private
        if not args:
            raise ValueError(
                "test_update_profile requires at least one of: bio, avatar_path, is_private"
            )
        result = execute_update_profile(browser, args=args)
        print(f"[+] update_profile result: {result}")
        return result
    finally:
        browser.close()


def test_upload(
    file_path: str,
    *,
    caption: str = "",
    location: Optional[str] = None,
    alt_text: Optional[str] = None,
    hide_likes: bool = False,
    disable_comments: bool = False,
) -> Dict[str, Any]:
    """Run the upload action against a local media file."""
    browser = _open_browser()
    try:
        args: Dict[str, Any] = {"file_path": file_path, "caption": caption}
        if location:
            args["location"] = location
        if alt_text:
            args["alt_text"] = alt_text
        if hide_likes:
            args["hide_likes"] = True
        if disable_comments:
            args["disable_comments"] = True
        result = execute_upload(browser, args=args)
        print(f"[+] upload result: {result}")
        return result
    finally:
        browser.close()


# ─── CLI dispatch ──────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Local DrissionPage action harness")
    sub = p.add_subparsers(dest="action", required=False)

    sub.add_parser("warmup", help="Run a Warmup 2.0 session")

    p_up = sub.add_parser("update_profile", help="Run the update-profile action")
    p_up.add_argument("--bio", default=None)
    p_up.add_argument("--avatar", dest="avatar_path", default=None)
    p_up.add_argument(
        "--private", dest="is_private", action="store_true", default=None,
    )
    p_up.add_argument(
        "--public", dest="is_public", action="store_true", default=False,
    )

    p_ul = sub.add_parser("upload", help="Run the upload action")
    p_ul.add_argument("--file", dest="file_path", required=True)
    p_ul.add_argument("--caption", default="")
    p_ul.add_argument("--location", default=None)
    p_ul.add_argument("--alt-text", dest="alt_text", default=None)
    p_ul.add_argument("--hide-likes", action="store_true")
    p_ul.add_argument("--disable-comments", action="store_true")

    return p


def _main(argv: List[str]) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)

    if ns.action == "warmup":
        test_warmup()
    elif ns.action == "update_profile":
        is_private: Optional[bool]
        if ns.is_private:
            is_private = True
        elif ns.is_public:
            is_private = False
        else:
            is_private = None
        test_update_profile(
            bio=ns.bio, avatar_path=ns.avatar_path, is_private=is_private,
        )
    elif ns.action == "upload":
        test_upload(
            file_path=ns.file_path,
            caption=ns.caption,
            location=ns.location,
            alt_text=ns.alt_text,
            hide_likes=ns.hide_likes,
            disable_comments=ns.disable_comments,
        )
    else:
        # No subcommand → run whichever lines you've uncommented below.
        # (Default to a short warmup smoke test.)
        # ── Uncomment the call you want to run, then `python test_actions_local.py` ──
        test_warmup()
        # test_update_profile(bio="testing locally — Warmup 2.0 baseline")
        # test_update_profile(is_private=False)
        # test_update_profile(avatar_path="/absolute/path/under/MEDIA_ROOT/avatar.jpg")
        # test_upload(file_path="/absolute/path/under/MEDIA_ROOT/clip.mp4", caption="Hi")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )
    raise SystemExit(_main(sys.argv[1:]))
