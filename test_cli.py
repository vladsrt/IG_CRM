"""
Interactive CLI Test Harness
----------------------------
Stand-alone, terminal-driven tester for every DrissionPage action in
this project — plus a no-DB FFmpeg uniqueization smoke test. Bypasses
Celery, the orchestrator, and the database.

How to use
~~~~~~~~~~
    1. Paste your IG cookies into ``COOKIES`` below (same shape that
       ``InstagramBrowser.inject_cookies`` expects).
    2. Run:    python test_cli.py
    3. The script asks (in order):
         a. whether to use a proxy
         b. if yes — protocol (http/https/socks4/socks5)
         c. if yes — proxy string ``IP:PORT:USER:PASS`` (the script
            converts it to ``protocol://USER:PASS@IP:PORT``)
         d. operating-system platform to spoof (windows/macos/linux)
       The final proxy URL and the resolved User-Agent are printed
       BEFORE Chromium boots so you can sanity-check.
    4. Pick from the numbered menu. The menu loops until [0] Exit.

Behaviour notes for QA
~~~~~~~~~~~~~~~~~~~~~~
* **Update Profile** has three distinct code paths the CLI exercises
  separately:
    - Bio only           (option 2) — fires the form Submit + waits
                                      for the "Profile saved." toast.
    - Avatar only        (option 3) — file injection is asynchronous;
                                      "Profile photo added." toast +
                                      Cancel-on-dialog handles persistence.
                                      Submit is intentionally NOT clicked.
    - Bio + Avatar       (option 6) — combined: avatar dialog is
                                      cancelled, then bio + Submit fire.
* **Upload** uses the left-rail Create flow
  (``navigate_left_rail("create")``) with mandatory hover-hydration
  and a 3-layer interruption-modal sweep at the start. The
  empty-state profile-Reels fallback has been removed.
* **Warmup 2.0** uses the same left-rail navigation for the Reels
  tab. Probabilities are hard-pinned: 30% like / 45% open comments /
  60-70% per-comment × 3-4 attempts. The action loop is
  time-bounded (``while time.monotonic() < end_time``) with weighted
  action selection.
* **Modal dismissal** is a 3-layer defense:
  L1 = text-button click (Not Now / Cancel / OK / Close);
  L2 = backdrop click at viewport (10, 10) if a dialog is still
       present;
  L3 = ESC key (``\\x1b`` first, then W3C ``\\ue00c``, then a
       JS-dispatched ``KeyboardEvent``).
  Option [7] runs three sweeps back-to-back so QA can see all three
  layers fire in isolation.
* **SVG clicks** are auto-resolved to their wrapping ``<a>`` /
  ``<button>`` / ``[role="button"]`` ancestor before any JS click
  (``_walk_up_to_clickable`` cascade: ``@role=button`` →
  ``tag:button`` → ``tag:a`` → ``parent(2)`` → ``parent(1)``).
  This is what makes the New-post left-rail icon clickable
  reliably — a JS click on the bare SVG throws
  ``TypeError: this.click is not a function``.
* **Browser** uses a fresh Chromium ``user_data_dir`` per launch
  (``user_data_dir="fresh"`` passed to ``InstagramBrowser``), so
  cached proxy decisions / ServiceWorkers / cookies from previous
  test runs cannot leak into the current one. The dir is auto-deleted
  on ``close()``.
* **FFmpeg uniqueization** runs at the production-locked noise level
  of 4 (regression-safe, deterministic).

Failure handling
~~~~~~~~~~~~~~~~
Every action call is wrapped in try/except. A failed test prints the
traceback and returns you to the menu — it never exits the CLI.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make `workers.*` and `app.*` importable when this file lives at repo root.
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.join(_HERE, "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from workers.actions.action_update_profile import execute_update_profile  # noqa: E402
from workers.actions.action_upload import execute_upload  # noqa: E402
from workers.actions.action_warmup import execute_warmup  # noqa: E402
from workers.core.behavior import dismiss_instagram_modals  # noqa: E402
from workers.core.browser_core import InstagramBrowser  # noqa: E402
from workers.utils.proxy_builder import parse_proxy_url  # noqa: E402
from workers.utils.ua_generator import (  # noqa: E402
    Platform,
    get_random_user_agent,
)

logger = logging.getLogger(__name__)


# ─── HARDCODE YOUR COOKIES HERE ────────────────────────────────────────
#   Same shape as ``InstagramBrowser.inject_cookies`` accepts.
#   Replace the example values below with your real session cookies.
# ───────────────────────────────────────────────────────────────────────
COOKIES: List[Dict[str, str]] = [
    {
        "name": "datr",
        "value": "hsdvabaLFXims1PZzQrFtubo",
        "domain": ".instagram.com",
        "path": "/"
    },
    {
        "name": "ds_user_id",
        "value": "66998291245",
        "domain": ".instagram.com",
        "path": "/"
    },
    {
        "name": "csrftoken",
        "value": "sljjAJ4xxm6KXrD4r0enyuZCYGvnkuzQ",
        "domain": ".instagram.com",
        "path": "/"
    },
    {
        "name": "ig_did",
        "value": "F9841847-BA9C-405E-947B-9D2D9D937D2C",
        "domain": ".instagram.com",
        "path": "/"
    },
    {
        "name": "wd",
        "value": "1806x967",
        "domain": ".instagram.com",
        "path": "/"
    },
    {
        "name": "mid",
        "value": "aW_HhgAEAAG-A2g3tEgSI9ruSOL7",
        "domain": ".instagram.com",
        "path": "/"
    },
    {
        "name": "sessionid",
        "value": "66998291245%3Aok3FfItZNeROyP%3A14%3AAYhCKuKte0AEzJsE-Ez4aH8-2YY4cQqAKu3Er4V5Qg",
        "domain": ".instagram.com",
        "path": "/"
    },
    {
        "name": "rur",
        "value": "\"LDC\\05466998291245\\0541809865019:01fe6b120c5bfb0d60544e123c0a3e8dc7140f66037453d599e2cfa2e931bca861395676\"",
        "domain": ".instagram.com",
        "path": "/"
    }
]

# Optional: paste a UA here to override the platform-pool selection.
# Leave empty to let the harness pick from the pool that matches the
# platform you choose at the prompt.
MANUAL_USER_AGENT: str = ""

HEADLESS: bool = False  # flip to True for unattended runs

_VALID_PROTOCOLS: tuple[str, ...] = ("http", "https", "socks4", "socks5")


# ─── Proxy parsing ─────────────────────────────────────────────────────
def parse_proxy(raw: str, *, protocol: str = "http") -> str:
    """Convert ``IP:PORT:USER:PASS`` → ``protocol://USER:PASS@IP:PORT``.

    The DataImpulse / IPRoyal / Bright Data style of distributing creds
    is a flat colon-separated quad. DrissionPage's proxy extension wants
    a real URL with a scheme. This helper does both jobs: it validates
    the four-field shape and prepends the chosen scheme.

    Raises:
        ValueError: if ``raw`` does not have exactly four colon-split
            non-empty fields, or if ``protocol`` is not one of
            ``http`` / ``https`` / ``socks4`` / ``socks5``.
    """
    proto = (protocol or "").strip().lower()
    if proto not in _VALID_PROTOCOLS:
        raise ValueError(
            f"unknown protocol {protocol!r}; expected one of {_VALID_PROTOCOLS}"
        )

    parts = [p.strip() for p in raw.strip().split(":")]
    if len(parts) != 4:
        raise ValueError(
            f"expected IP:PORT:USER:PASS (4 fields), got {len(parts)} from {raw!r}"
        )
    ip, port, user, pwd = parts
    if not all([ip, port, user, pwd]):
        raise ValueError(f"empty field in proxy string {raw!r}")
    if not port.isdigit():
        raise ValueError(f"port must be numeric, got {port!r}")

    url = f"{proto}://{user}:{pwd}@{ip}:{port}"

    # Round-trip through the canonical parser to fail fast on any
    # malformation that snuck past the field-count check.
    parse_proxy_url(url)
    return url


def _redact(proxy_url: str) -> str:
    if "@" not in proxy_url:
        return proxy_url
    scheme_split = proxy_url.split("://", 1)
    if len(scheme_split) == 2:
        scheme, rest = scheme_split
        return f"{scheme}://***@{rest.split('@', 1)[1]}"
    return f"***@{proxy_url.split('@', 1)[1]}"


# ─── Browser launcher ──────────────────────────────────────────────────
def _resolve_user_agent(platform: Platform) -> str:
    """Manual override > random pick from the platform pool."""
    if MANUAL_USER_AGENT.strip():
        return MANUAL_USER_AGENT.strip()
    return get_random_user_agent(platform)


def _open_browser(
    proxy: Optional[str], platform: Platform, user_agent: str,
) -> InstagramBrowser:
    """Single launcher path — no facade, no plain ChromiumPage.

    Both proxy and no-proxy modes go through ``InstagramBrowser``,
    which handles:

    * Generating the MV3 proxy extension when ``proxy`` is set.
    * Forcing ``--no-proxy-server`` when ``proxy`` is ``None`` so
      DrissionPage's stale INI proxy can't leak through.
    * Clearing DrissionPage's INI-cached proxy via ``set_proxy("")``
      regardless of branch — prevents the "no tunnel" symptom when
      DP otherwise injects ``--proxy-server=`` from a previous run.
    * Pinning a fresh Chromium ``user_data_dir`` per launch so the
      browser starts against a clean profile (no cached proxy
      decisions, no leftover ServiceWorker state, no stale cookies).

    All three of those guards landing on every test run is what makes
    the no-proxy / with-proxy cases reproducible.
    """
    if not COOKIES:
        print(
            "\n[!] WARNING: COOKIES list is empty — IG will redirect to "
            "the login page and the action will fail.\n"
            "    Open test_cli.py and paste your cookies into COOKIES.\n"
        )

    # Verification banner — printed BEFORE Chrome boots so the operator
    # can abort with Ctrl-C if something looks wrong.
    print("─" * 60)
    print(f"  Platform   : {platform.value}")
    print(f"  User-Agent : {user_agent}")
    print(f"  Proxy URL  : {_redact(proxy) if proxy else '(none)'}")
    print(f"  Profile    : fresh tempdir (per-run)")
    print("─" * 60)

    print(
        f"[*] Launching InstagramBrowser ("
        f"{'proxy=' + _redact(proxy) if proxy else 'no proxy'})…"
    )
    browser = InstagramBrowser(
        proxy_string=proxy,                 # None → no-proxy branch
        user_agent=user_agent,
        headless=HEADLESS,
        account_platform=platform.value,
        user_data_dir="fresh",              # always start clean in tests
    )
    browser.inject_cookies(COOKIES)
    return browser


# ─── FFmpeg uniqueization (standalone, no DB) ──────────────────────────
def _run_ffmpeg_uniqueize(input_path: str) -> str:
    """Run a single ffmpeg pass that re-encodes ``input_path`` with a
    unique fingerprint, light invisible noise, and stripped metadata —
    same pipeline as the Celery ``uniqueize_video`` task, minus the DB.

    Returns the absolute output path, written next to the input as
    ``<stem>__uniq_<8hex>.<ext>``.
    """
    src = Path(input_path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(f"input not found: {src}")

    # Re-use the production command builder so this stays in lockstep
    # with whatever uniqueize_video does in prod.
    from app.workers.media_tasks import _build_ffmpeg_cmd, _run_ffmpeg
    from app.core.config import settings

    fingerprint = uuid.uuid4().hex
    out = src.with_name(f"{src.stem}__uniq_{fingerprint[:8]}{src.suffix}")
    cmd = _build_ffmpeg_cmd(
        input_path=src,
        output_path=out,
        bitrate_bps=settings.FFMPEG_DEFAULT_BITRATE_BPS,
        noise_strength=4,
        preset="medium",
        fingerprint=fingerprint,
    )
    _run_ffmpeg(cmd)
    return str(out)


# ─── Test runners (each opens + closes its own browser) ────────────────
def _run_warmup(
    proxy: Optional[str], platform: Platform, user_agent: str,
    duration_minutes: float,
) -> None:
    browser = _open_browser(proxy, platform, user_agent)
    try:
        result = execute_warmup(browser, args={"duration_minutes": duration_minutes})
        print(f"[+] warmup result: {result}")
    finally:
        browser.close()


def _run_update_bio(
    proxy: Optional[str], platform: Platform, user_agent: str, bio: str,
) -> None:
    browser = _open_browser(proxy, platform, user_agent)
    try:
        result = execute_update_profile(browser, args={"bio": bio})
        print(f"[+] update bio result: {result}")
    finally:
        browser.close()


def _run_update_avatar(
    proxy: Optional[str], platform: Platform, user_agent: str,
    avatar_path: str,
) -> None:
    browser = _open_browser(proxy, platform, user_agent)
    try:
        result = execute_update_profile(browser, args={"avatar_path": avatar_path})
        print(f"[+] update avatar result: {result}")
    finally:
        browser.close()


def _run_update_bio_and_avatar(
    proxy: Optional[str], platform: Platform, user_agent: str,
    bio: str, avatar_path: str,
) -> None:
    """Exercise the combined bio + avatar path (Submit + save-marker fires).

    Splitting this from the single-field tests is deliberate: the
    avatar-only branch in ``execute_update_profile`` skips the form
    Submit entirely, while the combined branch still cancels the
    Change-Photo dialog and THEN submits the form for the bio. The
    two paths have completely different failure modes — keep them
    testable separately.
    """
    browser = _open_browser(proxy, platform, user_agent)
    try:
        result = execute_update_profile(
            browser, args={"bio": bio, "avatar_path": avatar_path},
        )
        print(f"[+] update bio + avatar result: {result}")
    finally:
        browser.close()


def _run_modal_dismissal(
    proxy: Optional[str], platform: Platform, user_agent: str,
) -> None:
    """Standalone exercise of ``dismiss_instagram_modals`` against the live feed.

    Loads ``https://www.instagram.com/`` with the configured cookies/
    proxy/UA, then runs the standalone modal sweep and reports the
    count. Useful when QA is hunting a specific interstitial — you
    can manually trigger it by visiting a fresh account, then run
    this option to confirm the sweep clears it.

    Several sweeps run back-to-back so you can also see whether IG
    queues a new modal immediately after the first one closes
    (it sometimes does — "Turn on notifications" → "Save your login
    info?" within a single render).
    """
    browser = _open_browser(proxy, platform, user_agent)
    try:
        print("[*] navigating to https://www.instagram.com/ …")
        browser.page.get("https://www.instagram.com/")
        time.sleep(5)  # let the feed + any modal render

        # Sweep 1 — full default budget (1.5s per selector).
        print("[*] sweep 1 (default 1.5s/selector) …")
        n1 = dismiss_instagram_modals(browser.page)
        print(f"    → cleared {n1} modal(s)")

        # Pause so any follow-up modal has time to mount.
        time.sleep(3)

        # Sweep 2 — short timeouts, simulating an in-loop sweep.
        print("[*] sweep 2 (in-loop 0.5s/selector) …")
        n2 = dismiss_instagram_modals(
            browser.page, per_selector_timeout_s=0.5, max_dismissals=2,
        )
        print(f"    → cleared {n2} modal(s)")

        # Sweep 3 — give it one more pass after another beat.
        time.sleep(3)
        print("[*] sweep 3 …")
        n3 = dismiss_instagram_modals(browser.page)
        print(f"    → cleared {n3} modal(s)")

        print(
            f"\n[+] modal dismissal test complete: total {n1 + n2 + n3} modal(s) "
            f"dismissed across 3 sweeps"
        )
    finally:
        browser.close()


def _run_upload(
    proxy: Optional[str], platform: Platform, user_agent: str,
    file_path: str, caption: str,
) -> None:
    browser = _open_browser(proxy, platform, user_agent)
    try:
        result = execute_upload(
            browser, args={"file_path": file_path, "caption": caption},
        )
        print(f"[+] upload result: {result}")
    finally:
        browser.close()


# ─── Interactive prompts ───────────────────────────────────────────────
def _prompt_yes_no(question: str) -> bool:
    while True:
        ans = input(f"{question} (y/n): ").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("    please answer y or n.")


def _prompt_protocol() -> str:
    options = " / ".join(_VALID_PROTOCOLS)
    while True:
        raw = input(f"Proxy protocol [{options}, default http]: ").strip().lower()
        if not raw:
            return "http"
        if raw in _VALID_PROTOCOLS:
            return raw
        print(f"    invalid — choose one of: {options}")


def _prompt_platform() -> Platform:
    options = " / ".join(p.value for p in Platform)
    while True:
        raw = input(f"Spoofed OS platform [{options}, default windows]: ").strip().lower()
        if not raw:
            return Platform.WINDOWS
        try:
            return Platform(raw)
        except ValueError:
            print(f"    invalid — choose one of: {options}")


def _prompt_proxy() -> Optional[str]:
    if not _prompt_yes_no("Do you want to use a proxy?"):
        return None

    protocol = _prompt_protocol()
    while True:
        raw = input("Proxy string (IP:PORT:USER:PASS): ").strip()
        try:
            converted = parse_proxy(raw, protocol=protocol)
        except ValueError as exc:
            print(f"[!] invalid proxy string: {exc}\n    try again.\n")
            continue
        print(f"[*] using proxy: {_redact(converted)}")
        return converted


def _prompt_float(label: str, default: float) -> float:
    raw = input(f"{label} [default {default}]: ").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[!] not a number — using default {default}")
        return default


def _prompt_nonempty(label: str) -> str:
    while True:
        val = input(f"{label}: ").strip()
        if val:
            return val
        print("    value cannot be empty.")


_MENU = """
─────────────────────────────────────────
  IG-CRM Local Action Test CLI
─────────────────────────────────────────
  [1] Test Warmup 2.0                (modal sweep + 30/45/60-70 probs)
  [2] Test Update Profile Bio        (Submit path)
  [3] Test Update Avatar             (async, NO Submit)
  [4] Test Upload Media              (left-rail Create)
  [5] Test FFmpeg Uniqueization
  [6] Test Update Bio + Avatar       (combined Submit path)
  [7] Test Modal Dismissal           (standalone sweep verification)
  [0] Exit
─────────────────────────────────────────
"""


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    )

    # ── Session-wide setup: proxy + platform + UA, asked once. ─────────
    proxy = _prompt_proxy()
    platform = _prompt_platform()
    user_agent = _resolve_user_agent(platform)

    print("\n[*] session resolved:")
    print(f"      proxy      = {_redact(proxy) if proxy else '(none)'}")
    print(f"      platform   = {platform.value}")
    print(f"      user_agent = {user_agent}\n")

    while True:
        print(_MENU)
        choice = input("Choose an option: ").strip()

        if choice == "0":
            print("bye.")
            return 0

        try:
            if choice == "1":
                duration = _prompt_float("Warmup duration (minutes)", 15.0)
                _run_warmup(proxy, platform, user_agent, duration)

            elif choice == "2":
                bio = _prompt_nonempty("New bio text")
                _run_update_bio(proxy, platform, user_agent, bio)

            elif choice == "3":
                avatar_path = _prompt_nonempty("Absolute path to avatar image")
                _run_update_avatar(proxy, platform, user_agent, avatar_path)

            elif choice == "4":
                file_path = _prompt_nonempty("Absolute path to media file")
                caption = input("Caption (blank for none): ").strip()
                _run_upload(proxy, platform, user_agent, file_path, caption)

            elif choice == "5":
                src = _prompt_nonempty("Absolute path to source video")
                out = _run_ffmpeg_uniqueize(src)
                print(f"[+] uniqueized → {out}")

            elif choice == "6":
                bio = _prompt_nonempty("New bio text")
                avatar_path = _prompt_nonempty("Absolute path to avatar image")
                _run_update_bio_and_avatar(
                    proxy, platform, user_agent, bio, avatar_path,
                )

            elif choice == "7":
                _run_modal_dismissal(proxy, platform, user_agent)

            else:
                print(f"[!] unknown option {choice!r}")

        except KeyboardInterrupt:
            print("\n[!] interrupted — back to menu.")
        except Exception as exc:
            # Per the spec: the CLI must NEVER exit on a single test
            # failure. Print the traceback and loop back to the menu.
            print(f"\n[!] test failed: {type(exc).__name__}: {exc}")
            traceback.print_exc()


if __name__ == "__main__":
    raise SystemExit(main())
