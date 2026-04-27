"""
Upload Action
-------------
Drives Instagram's web "Create → Post / Reel" flow against an already
authenticated ``InstagramBrowser`` and uploads the file at
``args["file_path"]``.

Public API
~~~~~~~~~~
``execute_upload(browser, args) -> dict`` — invoked by ``TaskExecutor``.
The browser is owned by the executor; this module never instantiates one
in production and never closes it.

Supported ``args``
~~~~~~~~~~~~~~~~~~
* ``file_path``         (required, str)  — absolute path to image or video
* ``caption``           (optional, str)  — caption text. Defaults to ``""``.
* ``location``          (optional, str)  — geotag query; first dropdown result is picked
* ``alt_text``          (optional, str)  — accessibility alt text
* ``hide_likes``        (optional, bool) — toggle "hide like and view counts"
* ``disable_comments``  (optional, bool) — toggle "turn off commenting"
* ``upload_timeout_s``  (optional, int)  — overall ceiling on the share→done wait
                                           (default 180s; raise for big videos)

Locator strategy
~~~~~~~~~~~~~~~~
All selectors target **semantic attributes** (``aria-label``, ``role``,
``placeholder``, visible text) rather than volatile React/Tailwind class
hashes. Each step tries multiple fallbacks so a single DOM tweak by IG
doesn't break the whole flow.
"""

from __future__ import annotations

import logging
import os
import random
import sys
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

# Allow `python action_upload.py` from the actions/ dir for the smoke-test.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from workers.core.browser_core import InstagramBrowser

logger = logging.getLogger(__name__)


# ── Tunables ────────────────────────────────────────────────────────────
_HOME_URL: str = "https://www.instagram.com/"
_DEFAULT_STEP_TIMEOUT_S: float = 20.0
_DEFAULT_UPLOAD_TIMEOUT_S: float = 180.0
_HUMAN_PAUSE_RANGE: tuple[float, float] = (1.0, 2.5)
_LONG_PAUSE_RANGE: tuple[float, float] = (2.5, 4.5)

_VIDEO_EXTS: frozenset[str] = frozenset(
    {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
)
_IMAGE_EXTS: frozenset[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp"}
)


# ── Errors ──────────────────────────────────────────────────────────────
class UploadActionError(RuntimeError):
    """Raised when a step in the upload flow cannot complete."""


# ── Human-mimicry helpers ───────────────────────────────────────────────
def _human_pause(rng: tuple[float, float] = _HUMAN_PAUSE_RANGE) -> None:
    time.sleep(random.uniform(*rng))


def _detect_media_kind(file_path: str) -> str:
    suffix = os.path.splitext(file_path)[1].lower()
    if suffix in _VIDEO_EXTS:
        return "video"
    if suffix in _IMAGE_EXTS:
        return "image"
    return "unknown"


# ── DrissionPage selector helpers ───────────────────────────────────────
def _find_first(
    page: Any,
    selectors: Iterable[str],
    *,
    timeout: float = _DEFAULT_STEP_TIMEOUT_S,
) -> Any | None:
    """Return the first selector that resolves to a real element, or ``None``.

    DrissionPage's ``page.ele(selector, timeout=N)`` blocks for up to ``N``
    seconds and returns the element (truthy) or ``None`` (falsy) on miss.
    To bound total wait, we split the overall ``timeout`` budget across
    candidate selectors.
    """
    selectors = list(selectors)
    if not selectors:
        return None
    per_sel = max(1.0, timeout / len(selectors))
    for sel in selectors:
        try:
            ele = page.ele(sel, timeout=per_sel)
        except Exception as exc:
            logger.debug("[upload] selector %r raised: %s", sel, exc)
            continue
        if ele:
            return ele
    return None


def _click_first(
    page: Any,
    selectors: Iterable[str],
    *,
    timeout: float = _DEFAULT_STEP_TIMEOUT_S,
    label: str,
) -> Any:
    """Find + click the first matching selector. Raises on miss / click failure."""
    ele = _find_first(page, selectors, timeout=timeout)
    if ele is None:
        raise UploadActionError(
            f"Could not locate {label!r} (tried {list(selectors)})"
        )
    try:
        ele.click()
    except Exception as exc:
        raise UploadActionError(
            f"Found {label!r} but click failed: {exc}"
        ) from exc
    return ele


def _step(label: str, fn: Callable[[], Any]) -> Any:
    """Wrap a step so failures carry their step name in the error chain.

    Lets ``UploadActionError`` propagate verbatim; wraps any other exception
    type so the executor (and thus the Celery task) gets a clear stack.
    """
    logger.info("[upload] step: %s", label)
    try:
        return fn()
    except UploadActionError:
        raise
    except Exception as exc:
        raise UploadActionError(
            f"Step {label!r} failed: {type(exc).__name__}: {exc}"
        ) from exc


# ── Step implementations ────────────────────────────────────────────────
def _navigate_home(browser: InstagramBrowser) -> None:
    browser.page.get(_HOME_URL)
    landed = _find_first(
        browser.page,
        [
            'css:svg[aria-label="New post"]',
            'css:svg[aria-label="Home"]',
        ],
        timeout=_DEFAULT_STEP_TIMEOUT_S,
    )
    if landed is None:
        raise UploadActionError("Home feed did not render — session may be invalid")
    _human_pause(_LONG_PAUSE_RANGE)


def _open_create_dialog(browser: InstagramBrowser) -> None:
    # The "Create" entry point lives on the left sidebar. Some accounts get
    # a submenu (Post / Reel / Story); others go straight to the file picker.
    _click_first(
        browser.page,
        [
            'css:svg[aria-label="New post"]',
            'css:a[href="#"] svg[aria-label="New post"]',
            'xpath://span[text()="Create"]',
            'text:Create',
        ],
        label="Create button",
    )
    _human_pause()

    # If the submenu appeared, pick "Post" — IG converts long videos to Reels
    # server-side, so "Post" works for both kinds.
    submenu_post = _find_first(
        browser.page,
        [
            'xpath://span[text()="Post"]',
            'xpath://*[@role="menuitem"][.//span[text()="Post"]]',
        ],
        timeout=4.0,
    )
    if submenu_post is not None:
        try:
            submenu_post.click()
            _human_pause()
        except Exception as exc:
            logger.debug("[upload] submenu Post click failed (%s); ignoring", exc)


def _inject_file(browser: InstagramBrowser, file_path: str) -> None:
    abs_path = os.path.abspath(file_path)
    if not os.path.isfile(abs_path):
        raise UploadActionError(f"Upload file does not exist: {abs_path}")

    # Hidden file input — we never click "Select from computer" because that
    # opens the OS file dialog which DrissionPage cannot drive.
    file_input = _find_first(
        browser.page,
        [
            'css:input[type="file"][accept*="video"]',
            'css:input[type="file"][accept*="image"]',
            'css:form[role="presentation"] input[type="file"]',
            'css:input[type="file"]',
        ],
        timeout=_DEFAULT_STEP_TIMEOUT_S,
    )
    if file_input is None:
        raise UploadActionError("Hidden <input type=file> not found in DOM")

    try:
        file_input.input(abs_path)
    except Exception as exc:
        raise UploadActionError(
            f"file_input.input({abs_path!r}) failed: {exc}"
        ) from exc

    _human_pause(_LONG_PAUSE_RANGE)


def _dismiss_video_reels_notice(browser: InstagramBrowser) -> None:
    """When uploading a video IG sometimes interrupts with an OK-modal."""
    ok_btn = _find_first(
        browser.page,
        [
            'xpath://button[normalize-space()="OK"]',
            'xpath://div[@role="button" and normalize-space()="OK"]',
        ],
        timeout=4.0,
    )
    if ok_btn is not None:
        try:
            ok_btn.click()
            _human_pause()
        except Exception as exc:
            logger.debug("[upload] OK-modal click failed (%s); ignoring", exc)


def _click_next(browser: InstagramBrowser, *, label: str) -> None:
    _click_first(
        browser.page,
        [
            'xpath://div[@role="button" and normalize-space()="Next"]',
            'xpath://button[normalize-space()="Next"]',
            'text:Next',
        ],
        label=label,
        timeout=_DEFAULT_STEP_TIMEOUT_S,
    )
    _human_pause()


def _write_caption(browser: InstagramBrowser, caption: str) -> None:
    if not caption:
        return
    caption_box = _find_first(
        browser.page,
        [
            'css:div[aria-label="Write a caption..."][contenteditable="true"]',
            'css:div[contenteditable="true"][aria-label="Write a caption..."]',
            'css:div[role="textbox"][aria-label="Write a caption..."]',
            'xpath://div[@aria-label="Write a caption..." and @contenteditable="true"]',
        ],
        timeout=_DEFAULT_STEP_TIMEOUT_S,
    )
    if caption_box is None:
        raise UploadActionError("Caption editor not found")
    try:
        caption_box.click()
        _human_pause((0.4, 1.0))
        caption_box.input(caption)
    except Exception as exc:
        raise UploadActionError(f"Caption input failed: {exc}") from exc
    _human_pause()


def _add_location(browser: InstagramBrowser, location: str) -> None:
    if not location:
        return
    loc_input = _find_first(
        browser.page,
        [
            'css:input[placeholder="Add location"]',
            'css:input[name="creation-location-input"]',
            'xpath://input[@placeholder="Add location"]',
        ],
        timeout=8.0,
    )
    if loc_input is None:
        logger.warning("[upload] location input not found — skipping geotag")
        return

    try:
        loc_input.click()
        _human_pause((0.3, 0.8))
        loc_input.input(location)
    except Exception as exc:
        logger.warning("[upload] location input typing failed (%s); skipping", exc)
        return

    # Wait for the suggestions list to populate, then click the first hit.
    _human_pause((1.5, 2.5))
    suggestion = _find_first(
        browser.page,
        [
            'xpath://div[@role="button"]//div[contains(@class,"x9f619")][1]',
            'xpath://ul//button[1]',
            'xpath://div[@role="dialog"]//button[1]',
        ],
        timeout=5.0,
    )
    if suggestion is None:
        logger.warning("[upload] no location suggestion appeared — skipping pick")
        return
    try:
        suggestion.click()
        _human_pause()
    except Exception as exc:
        logger.warning("[upload] location suggestion click failed (%s); skipping", exc)


def _add_alt_text(browser: InstagramBrowser, alt_text: str) -> None:
    if not alt_text:
        return
    accessibility_btn = _find_first(
        browser.page,
        [
            'xpath://div[@role="button"][.//span[text()="Accessibility"]]',
            'xpath://span[text()="Accessibility"]/ancestor::div[@role="button"][1]',
            'text:Accessibility',
        ],
        timeout=6.0,
    )
    if accessibility_btn is None:
        logger.warning("[upload] Accessibility section not found — skipping alt text")
        return
    try:
        accessibility_btn.click()
        _human_pause()
    except Exception as exc:
        logger.warning("[upload] Accessibility expand failed (%s); skipping", exc)
        return

    alt_input = _find_first(
        browser.page,
        [
            'css:input[aria-label="Write alt text..."]',
            'css:textarea[aria-label="Write alt text..."]',
            'css:input[placeholder="Write alt text..."]',
            'css:textarea[placeholder="Write alt text..."]',
        ],
        timeout=6.0,
    )
    if alt_input is None:
        logger.warning("[upload] alt text input not found — skipping")
        return
    try:
        alt_input.click()
        _human_pause((0.3, 0.8))
        alt_input.input(alt_text)
        _human_pause()
    except Exception as exc:
        logger.warning("[upload] alt text input failed (%s); skipping", exc)


def _toggle_advanced_settings(
    browser: InstagramBrowser,
    *,
    hide_likes: bool,
    disable_comments: bool,
) -> None:
    if not (hide_likes or disable_comments):
        return

    advanced_btn = _find_first(
        browser.page,
        [
            'xpath://div[@role="button"][.//span[text()="Advanced settings"]]',
            'xpath://span[text()="Advanced settings"]/ancestor::div[@role="button"][1]',
            'text:Advanced settings',
        ],
        timeout=6.0,
    )
    if advanced_btn is None:
        logger.warning("[upload] Advanced settings section not found — skipping toggles")
        return
    try:
        advanced_btn.click()
        _human_pause()
    except Exception as exc:
        logger.warning("[upload] Advanced settings expand failed (%s); skipping", exc)
        return

    # IG renders the toggles as either <input role=switch> or <div role=switch>.
    # We grab them all and pick by label proximity.
    if hide_likes:
        toggle = _find_first(
            browser.page,
            [
                'xpath://*[contains(text(),"Hide like and view counts")]'
                '/ancestor::div[.//*[@role="switch" or @type="checkbox"]][1]'
                '//*[@role="switch" or @type="checkbox"]',
                'css:input[name="hide_like_and_view_counts"]',
            ],
            timeout=4.0,
        )
        if toggle is not None:
            try:
                toggle.click()
                _human_pause((0.3, 0.7))
            except Exception as exc:
                logger.warning("[upload] hide_likes toggle failed (%s)", exc)
        else:
            logger.warning("[upload] hide_likes toggle not found — skipping")

    if disable_comments:
        toggle = _find_first(
            browser.page,
            [
                'xpath://*[contains(text(),"Turn off commenting")]'
                '/ancestor::div[.//*[@role="switch" or @type="checkbox"]][1]'
                '//*[@role="switch" or @type="checkbox"]',
                'css:input[name="not_ad"]',
            ],
            timeout=4.0,
        )
        if toggle is not None:
            try:
                toggle.click()
                _human_pause((0.3, 0.7))
            except Exception as exc:
                logger.warning("[upload] disable_comments toggle failed (%s)", exc)
        else:
            logger.warning("[upload] disable_comments toggle not found — skipping")


def _click_share(browser: InstagramBrowser) -> None:
    _click_first(
        browser.page,
        [
            'xpath://div[@role="button" and normalize-space()="Share"]',
            'xpath://button[normalize-space()="Share"]',
            'text:Share',
        ],
        label="Share button",
        timeout=_DEFAULT_STEP_TIMEOUT_S,
    )
    _human_pause()


def _wait_for_completion(browser: InstagramBrowser, timeout_s: float) -> str:
    """Block until IG confirms the post landed. Returns the matched marker text."""
    deadline = time.monotonic() + timeout_s
    confirm_selectors: List[str] = [
        'xpath://*[contains(text(),"Your reel has been shared")]',
        'xpath://*[contains(text(),"Your post has been shared")]',
        'xpath://*[contains(text(),"Post shared")]',
        'xpath://*[contains(text(),"Reel shared")]',
        'xpath://div[@role="button" and normalize-space()="Done"]',
        'xpath://button[normalize-space()="Done"]',
    ]
    error_selectors: List[str] = [
        'xpath://*[contains(text(),"Something went wrong")]',
        'xpath://*[contains(text(),"could not be shared")]',
        'xpath://*[contains(text(),"Try again")]',
    ]

    while time.monotonic() < deadline:
        for sel in confirm_selectors:
            try:
                ele = browser.page.ele(sel, timeout=2)
            except Exception:
                continue
            if ele:
                logger.info("[upload] completion marker matched: %s", sel)
                _human_pause((0.5, 1.2))
                return sel

        for sel in error_selectors:
            try:
                err_ele = browser.page.ele(sel, timeout=1)
            except Exception:
                continue
            if err_ele:
                txt = ""
                try:
                    txt = err_ele.text or ""
                except Exception:
                    pass
                raise UploadActionError(
                    f"Instagram reported an upload error: {txt or sel}"
                )

        time.sleep(2.0)

    raise UploadActionError(
        f"Upload did not complete within {timeout_s:.0f}s — no confirmation marker"
    )


# ── Public entrypoint ───────────────────────────────────────────────────
def execute_upload(
    browser: InstagramBrowser, args: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Drive Instagram's Create-Post flow end-to-end.

    The browser is owned by the caller (``TaskExecutor``); this function
    never instantiates a new one and never calls ``browser.close()``. Step
    failures propagate as ``UploadActionError`` so the executor can mark
    the parent Task FAILED with a useful message.
    """
    args = args or {}

    file_path = args.get("file_path")
    if not file_path or not isinstance(file_path, str):
        raise UploadActionError("upload action requires a string 'file_path' arg")

    abs_path = os.path.abspath(file_path)
    media_kind = _detect_media_kind(abs_path)
    caption = str(args.get("caption") or "")
    location = str(args.get("location") or "").strip()
    alt_text = str(args.get("alt_text") or "").strip()
    hide_likes = bool(args.get("hide_likes", False))
    disable_comments = bool(args.get("disable_comments", False))
    upload_timeout_s = float(args.get("upload_timeout_s", _DEFAULT_UPLOAD_TIMEOUT_S))

    logger.info(
        "[upload] starting file=%s kind=%s caption_len=%d location=%r alt_len=%d "
        "hide_likes=%s disable_comments=%s",
        abs_path, media_kind, len(caption), location, len(alt_text),
        hide_likes, disable_comments,
    )

    _step("navigate to home feed",
          lambda: _navigate_home(browser))

    _step("open Create dialog",
          lambda: _open_create_dialog(browser))

    _step("inject file into hidden input",
          lambda: _inject_file(browser, abs_path))

    if media_kind == "video":
        _step("dismiss reels-notice modal (if present)",
              lambda: _dismiss_video_reels_notice(browser))

    _step("click Next (crop)",
          lambda: _click_next(browser, label="Next button (crop step)"))

    _step("click Next (filter/trim)",
          lambda: _click_next(browser, label="Next button (filter step)"))

    _step("write caption",
          lambda: _write_caption(browser, caption))

    _step("add location",
          lambda: _add_location(browser, location))

    _step("add alt text",
          lambda: _add_alt_text(browser, alt_text))

    _step("toggle advanced settings",
          lambda: _toggle_advanced_settings(
              browser,
              hide_likes=hide_likes,
              disable_comments=disable_comments,
          ))

    _step("click Share",
          lambda: _click_share(browser))

    marker = _step("wait for completion",
                   lambda: _wait_for_completion(browser, upload_timeout_s))

    logger.info("[upload] success file=%s", abs_path)
    return {
        "action": "upload",
        "status": "success",
        "file": file_path,
        "media_kind": media_kind,
        "caption_length": len(caption),
        "location": location or None,
        "alt_text_length": len(alt_text),
        "hide_likes": hide_likes,
        "disable_comments": disable_comments,
        "completion_marker": marker,
    }


# ── Standalone smoke-test (not used in production) ──────────────────────
_SMOKE_TEST_PROXY: str = "8d1f77cde74f6dffffea__cr.us:80fe1a46ee235b27@gw.dataimpulse.com:823"

_SMOKE_TEST_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

_SMOKE_TEST_COOKIES = [
    {"domain": ".instagram.com", "name": "ps_n",       "value": "1",                                                                                                                                "path": "/"},
    {"domain": ".instagram.com", "name": "datr",       "value": "4PnXaAUhBF6H4VaGhlf1j1g0",                                                                                                        "path": "/"},
    {"domain": ".instagram.com", "name": "ds_user_id", "value": "77203602829",                                                                                                                      "path": "/"},
    {"domain": ".instagram.com", "name": "csrftoken",  "value": "30jG0bFQ9XloSZWb1TK7BaoYs83jmkzU",                                                                                               "path": "/"},
    {"domain": ".instagram.com", "name": "mid",        "value": "aNf54AAEAAGwnGqXcIkj68TtKnh4",                                                                                                    "path": "/"},
    {"domain": ".instagram.com", "name": "sessionid",  "value": "77203602829%3ABV1b0aNRX4sWwg%3A12%3AAYhp6okkj6yOVAozVcwhIbkgh3bDMnloQ1mlUSz6Xzk", "path": "/"},
    {"domain": ".instagram.com", "name": "ps_l",       "value": "1",                                                                                                                                "path": "/"},
    {"domain": ".instagram.com", "name": "dpr",        "value": "1",                                                                                                                                "path": "/"},
    {"domain": ".instagram.com", "name": "rur",        "value": '"NHA\\05477203602829\\0541808322467:01fe0b0984dcba8b6cc3f7dfa5743dd979aa75cc0cfba7d1c44eef3d301cffab39339cb7"', "path": "/"},
]


def _run_standalone_smoke_test() -> None:
    print("=" * 55)
    print("  Instagram Worker - Standalone Upload Smoke Test")
    print("=" * 55)

    test_file = os.environ.get("UPLOAD_TEST_FILE", "/tmp/test_reel.mp4")
    if not os.path.isfile(test_file):
        print(f"[!] Smoke test file not found: {test_file}")
        print("    Set UPLOAD_TEST_FILE env var to a real media file.")
        return

    browser: InstagramBrowser | None = None
    try:
        browser = InstagramBrowser(
            proxy_string=_SMOKE_TEST_PROXY,
            user_agent=_SMOKE_TEST_USER_AGENT,
            headless=False,
        )
        browser.inject_cookies(_SMOKE_TEST_COOKIES)
        result = execute_upload(
            browser,
            args={
                "file_path": test_file,
                "caption": "Smoke test upload via CRM worker",
            },
        )
        print(f"[+] Upload result: {result}")
    except Exception as exc:
        import traceback
        print(f"[!] Smoke test failed: {exc}")
        traceback.print_exc()
    finally:
        if browser is not None:
            browser.close()
        print("=" * 55)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    _run_standalone_smoke_test()
