"""
upload action.
uploads a file to instagram.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

# Allow `python action_upload.py` from the actions/ dir for the smoke-test.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from app.core.config import settings
from workers.core.behavior import (
    HumanBehaviorEngine,
    ClickVerificationError,
    safe_coordinate_click,
)
from workers.core.browser_core import InstagramBrowser
from workers.core.safety import UnsafePathError, resolve_within_media_root

logger = logging.getLogger(__name__)


# safe_coordinate_click is imported from workers.core.behavior


# settings
_HOME_URL: str = "https://www.instagram.com/"
_DEFAULT_STEP_TIMEOUT_S: float = 20.0
_DEFAULT_UPLOAD_TIMEOUT_S: float = 180.0
_FILE_UPLOAD_PROCESSING_S: tuple[float, float] = (4.0, 8.0)
_AFTER_NEXT_PAUSE_S: tuple[float, float] = (1.4, 2.6)


# errors
class UploadActionError(RuntimeError):
    """Raised when a step in the upload flow cannot complete."""


# helpers
def _find_first(
    page: Any,
    selectors: Iterable[str],
    *,
    timeout: float = _DEFAULT_STEP_TIMEOUT_S,
) -> Any | None:
    """find first element from selectors."""
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


def _humanized_click_first(
    page: Any,
    behavior: HumanBehaviorEngine,
    selectors: Iterable[str],
    *,
    timeout: float = _DEFAULT_STEP_TIMEOUT_S,
    label: str,
    safe: bool = True,
) -> Any:
    """click first element found."""
    ele = _find_first(page, selectors, timeout=timeout)
    if ele is None:
        raise UploadActionError(
            f"Could not locate {label!r} (tried {list(selectors)})"
        )
    try:
        if safe:
            behavior.safe_click_button(ele)
        else:
            behavior.click(ele)
    except Exception as exc:
        raise UploadActionError(
            f"Found {label!r} but click failed: {exc}"
        ) from exc
    return ele


def _step(label: str, fn: Callable[[], Any]) -> Any:
    """run step and catch errors."""
    logger.info("[upload] step: %s", label)
    try:
        return fn()
    except UploadActionError:
        raise
    except Exception as exc:
        raise UploadActionError(
            f"Step {label!r} failed: {type(exc).__name__}: {exc}"
        ) from exc


def _arm_file_upload(browser: InstagramBrowser, abs_path: str) -> None:
    """setup file upload."""
    try:
        browser.page.set.upload_files(abs_path)
    except Exception as exc:
        raise UploadActionError(
            f"page.set.upload_files({abs_path!r}) failed: {exc}"
        ) from exc
    logger.info("[upload] file-dialog interception armed for %s", abs_path)


# steps
def _navigate_home(browser: InstagramBrowser, behavior: HumanBehaviorEngine) -> None:
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

    # Skim the feed briefly before doing anything (real users don't
    # load IG and instantly hit Create).
    behavior.read_pause(content_length=None)
    behavior.micro_scroll()


def _wait_for_create_modal(
    browser: InstagramBrowser, *, timeout_s: float = 10.0
) -> bool:
    """check if create modal is open."""
    deadline = time.monotonic() + timeout_s
    selectors: list[str] = [
        'xpath://div[@role="heading" and @aria-level="1" and contains(.,"Create new post")]',
        'xpath://*[@aria-level="1" and @role="heading" and contains(.,"Create new")]',
        'xpath://h1[contains(.,"Create new post")]',
        'xpath://button[normalize-space()="Select from computer"]',
    ]
    while time.monotonic() < deadline:
        if _find_first(browser.page, selectors, timeout=1.5) is not None:
            return True
    return False


def _open_create_dialog_via_left_rail(
    browser: InstagramBrowser, behavior: HumanBehaviorEngine
) -> None:
    """open create post modal."""
    logger.info("[upload] opening Create modal via left-rail")

    if not behavior.navigate_left_rail("create"):
        raise UploadActionError(
            "left-rail Create click failed — rail may not have hydrated"
        )

    # Optional Post / Reel / Story submenu — click "Post" if it shows.
    submenu_post = _find_first(
        browser.page,
        [
            'xpath://*[@role="menuitem"][.//span[normalize-space()="Post"]]',
            'xpath://span[normalize-space()="Post"]',
        ],
        timeout=3.0,
    )
    if submenu_post is not None:
        try:
            behavior.safe_click(submenu_post)
            behavior.idle(0.5, 1.1)
        except Exception as exc:
            logger.debug("[upload] submenu Post click failed (%s); ignoring", exc)

    if not _wait_for_create_modal(browser, timeout_s=10.0):
        raise UploadActionError(
            "'Create new post' modal did not appear after left-rail Create click"
        )


def _click_select_from_computer(
    browser: InstagramBrowser, behavior: HumanBehaviorEngine
) -> None:
    """click select from computer."""
    _humanized_click_first(
        browser.page,
        behavior,
        [
            'xpath://button[normalize-space()="Select from computer"]',
            'xpath://*[@role="button" and normalize-space()="Select from computer"]',
        ],
        label="Select from computer",
        timeout=_DEFAULT_STEP_TIMEOUT_S,
        safe=True,
    )


def _set_crop_to_original(
    browser: InstagramBrowser, behavior: HumanBehaviorEngine
) -> None:
    """set crop to original."""
    crop_trigger = _find_first(
        browser.page,
        [
            'css:svg[aria-label="Select crop"]',
            'xpath://*[@aria-label="Select crop"]',
        ],
        timeout=6.0,
    )
    if crop_trigger is None:
        logger.info("[upload] no 'Select crop' trigger — skipping (likely already-Original)")
        return

    try:
        behavior.click(crop_trigger)
    except Exception as exc:
        logger.warning("[upload] crop trigger click failed (%s); skipping crop step", exc)
        return
    behavior.idle(0.6, 1.2)

    original_opt = _find_first(
        browser.page,
        [
            'xpath://span[normalize-space()="Original"]',
            'xpath://*[@role="button"][.//span[normalize-space()="Original"]]',
        ],
        timeout=4.0,
    )
    if original_opt is None:
        logger.warning("[upload] 'Original' crop option not found — leaving default AR")
        return
    try:
        behavior.safe_click_button(original_opt, settle_s=0.4)
    except Exception as exc:
        logger.warning("[upload] 'Original' click failed (%s); leaving default AR", exc)


def _click_next(
    browser: InstagramBrowser, behavior: HumanBehaviorEngine, *, label: str
) -> None:
    _humanized_click_first(
        browser.page,
        behavior,
        [
            'xpath://div[@role="button" and normalize-space()="Next"]',
            'xpath://button[normalize-space()="Next"]',
            'css:div[role="button"]:has-text("Next")',
            'text:Next',
        ],
        label=label,
        timeout=_DEFAULT_STEP_TIMEOUT_S,
        safe=True,
    )
    behavior.idle(*_AFTER_NEXT_PAUSE_S)


def _write_caption(
    browser: InstagramBrowser, behavior: HumanBehaviorEngine, caption: str
) -> None:
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

    # Wipe any pre-fill IG might have hydrated (rare, but happens with
    # collab mentions). Same JS-clear that the bio editor uses.
    try:
        behavior.clear_input_field(caption_box, focus_first=True)
    except Exception as exc:
        logger.debug("[upload] caption pre-clear failed (%s); proceeding anyway", exc)

    try:
        behavior.type_into(caption_box, caption, focus_first=False)
    except Exception as exc:
        raise UploadActionError(f"Caption input failed: {exc}") from exc

    # Re-read what was typed before moving on — natural beat that
    # scales with caption length.
    behavior.read_pause(content_length=len(caption))


def _click_share(
    browser: InstagramBrowser, behavior: HumanBehaviorEngine
) -> None:
    """click share button."""
    share_selectors = [
        'xpath://div[@role="button" and normalize-space()="Share"]',
        'xpath://button[normalize-space()="Share"]',
        'css:div[role="button"]:has-text("Share")',
        'text:Share',
    ]

    # check for modals right before clicking share so they don't block the click
    # modal that just appeared.
    try:
        behavior.dismiss_interruptions()
    except Exception as exc:
        logger.debug(
            "[upload] in-share dismiss_interruptions raised (%s); proceeding", exc,
        )

    share_btn = _find_first(
        browser.page, share_selectors, timeout=_DEFAULT_STEP_TIMEOUT_S,
    )
    if share_btn is None:
        raise UploadActionError(
            f"Could not locate 'Share button' (tried {share_selectors})"
        )

    # try normal click first
    try:
        behavior.safe_click_button(share_btn)
        logger.info("[upload] Share clicked via safe_click_button")
        return
    except Exception as exc:
        logger.warning(
            "[upload] safe_click_button on Share failed (%s); falling back to "
            "ele.click(by_js=True) — assuming overlay interception",
            exc,
        )

    # fallback: use javascript click if normal click fails.
    # sweep again just in case a popup appeared when click failed.
    # coordinate click sometimes shifts focus and a previously-hidden
    # nag modal mounts in the same beat.
    try:
        behavior.dismiss_interruptions()
    except Exception as exc:
        logger.debug(
            "[upload] pre-JS-click dismiss_interruptions raised (%s); proceeding",
            exc,
        )

    try:
        share_btn.click(by_js=True)
        logger.info("[upload] Share clicked via JS click fallback")
    except Exception as exc:
        raise UploadActionError(
            f"Share click failed via both safe_click_button and JS click: {exc}"
        ) from exc


def _wait_for_completion(browser: InstagramBrowser, timeout_s: float) -> str:
    """wait for upload to finish."""
    deadline = time.monotonic() + timeout_s
    confirm_selectors: List[str] = [
        'xpath://h3[contains(.,"Your reel has been shared")]',
        'xpath://h3[contains(.,"Your post has been shared")]',
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


def _click_done(
    browser: InstagramBrowser, behavior: HumanBehaviorEngine
) -> None:
    """click done."""
    done_btn = _find_first(
        browser.page,
        [
            'xpath://div[@role="button" and normalize-space()="Done"]',
            'xpath://button[normalize-space()="Done"]',
            'css:div[role="button"]:has-text("Done")',
        ],
        timeout=6.0,
    )
    if done_btn is None:
        logger.info("[upload] no Done button to dismiss (modal probably auto-closed)")
        return
    try:
        behavior.safe_click_button(done_btn)
    except Exception as exc:
        logger.debug("[upload] Done click failed (%s); ignoring", exc)


# main entry point
def execute_upload(
    browser: InstagramBrowser, args: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    uploads a post or reel to instagram.
    returns dict with result.
    """
    args = args or {}

    file_path = args.get("file_path")
    if not file_path or not isinstance(file_path, str):
        raise UploadActionError("upload action requires a string 'file_path' arg")

    # CRITICAL-4 — validate the path BEFORE doing anything browser-side
    # so we surface the security error cleanly without launching a nav.
    try:
        abs_path = resolve_within_media_root(file_path, settings.MEDIA_ROOT)
    except UnsafePathError as exc:
        raise UploadActionError(f"unsafe upload file_path: {exc}") from exc

    caption = str(args.get("caption") or "")
    location = str(args.get("location") or "")
    hide_likes = bool(args.get("hide_likes", False))
    disable_comments = bool(args.get("disable_comments", False))
    upload_timeout_s = float(args.get("upload_timeout_s", _DEFAULT_UPLOAD_TIMEOUT_S))

    logger.info(
        "[upload] starting file=%s caption_len=%d location=%r",
        abs_path, len(caption), location or "(none)",
    )

    page = browser.page
    # One engine per upload session — used for idle pauses and typing only.
    behavior = HumanBehaviorEngine(page)

    # clear popups before starting
    try:
        behavior.dismiss_interruptions()
    except Exception as exc:
        logger.debug(
            "[upload] dismiss_interruptions raised unexpectedly (%s); continuing",
            exc,
        )

    # go to home page
    _step("navigate to home feed",
          lambda: _navigate_home(browser, behavior))

    # Sweep again — the home feed render itself often triggers a modal.
    try:
        behavior.dismiss_interruptions()
    except Exception as exc:
        logger.debug(
            "[upload] post-home dismiss_interruptions raised (%s); continuing", exc
        )

    # Step 1: Open the create modal
    logger.info("[upload] step 1: Click Create (verified)")
    _VERIFY_CREATE_MODAL = (
        'xpath://div[@role="heading" and contains(.,"Create new post")]'
    )
    try:
        safe_coordinate_click(
            page,
            'css:svg[aria-label="New post"]',
            timeout=10,
            verify_locator=_VERIFY_CREATE_MODAL,
            max_retries=3,
            verify_timeout=5.0,
        )
    except ClickVerificationError:
        raise UploadActionError(
            "Step 1 FAILED: clicked 'New post' but 'Create new post' modal "
            "never appeared after 3 retries"
        )
    behavior.idle(1.0, 2.0)

    # Step 2: Set up native file upload and click select
    logger.info("[upload] step 2: File injection")
    _step("arm DrissionPage native file-dialog interception",
          lambda: _arm_file_upload(browser, abs_path))

    _step("click 'Select from computer'",
          lambda: _click_select_from_computer(browser, behavior))

    # IG processes the upload client-side before the crop UI appears.
    behavior.idle(*_FILE_UPLOAD_PROCESSING_S)

    # Sweep — fresh accounts hit a "Video posts are now shared as
    # reels" informational modal right after upload processing.
    try:
        behavior.dismiss_interruptions()
    except Exception as exc:
        logger.debug(
            "[upload] post-processing dismiss raised (%s); continuing", exc,
        )

    # Step 3-4: Crop to original if needed
    logger.info("[upload] step 3: Click Crop icon")
    if not safe_coordinate_click(page, 'css:svg[aria-label="Select crop"]', timeout=6):
        logger.info("[upload] no 'Select crop' icon — skipping")
    else:
        behavior.idle(0.6, 1.2)
        logger.info("[upload] step 4: Select Original")
        if not safe_coordinate_click(page, 't:span@text()=Original', timeout=4):
            logger.warning("[upload] 'Original' not found — leaving default AR")
        behavior.idle(0.5, 1.0)

    # Step 5: Advance past crop screen
    logger.info("[upload] step 5: Click Next (post-crop, verified)")
    _VERIFY_POST_CROP = (
        'xpath://div[@role="heading" and ('
        'contains(.,"Edit") or contains(.,"Filter") or '
        'contains(.,"Cover photo") or contains(.,"Adjustments")'
        ')]'
    )
    try:
        safe_coordinate_click(
            page,
            't:div@text()=Next',
            timeout=10,
            verify_locator=_VERIFY_POST_CROP,
            max_retries=3,
            verify_timeout=5.0,
        )
    except ClickVerificationError:
        raise UploadActionError(
            "Step 5 FAILED: clicked 'Next' but did not advance past crop "
            "screen (no Edit/Filter/Cover heading found)"
        )
    behavior.idle(*_AFTER_NEXT_PAUSE_S)

    # Step 6-7: Next past filters to caption screen
    logger.info("[upload] step 6-7: Click Next to caption (verified)")
    _VERIFY_CAPTION_SCREEN = 'css:div[aria-label="Write a caption..."]'
    page.wait(2.0)  # Let React render the optional panel

    # Try clicking Next — if it's there, we need it. If not, we might
    # already be on the caption screen.
    try:
        safe_coordinate_click(
            page,
            't:div@text()=Next',
            timeout=6,
            verify_locator=_VERIFY_CAPTION_SCREEN,
            max_retries=3,
            verify_timeout=5.0,
        )
    except ClickVerificationError:
        # Next was clicked but caption didn't appear — fatal desync.
        raise UploadActionError(
            "Step 7 FAILED: clicked 'Next' but caption textarea never appeared"
        )

    # Double-check: even if we didn't click Next (it wasn't there),
    # the caption box MUST be visible before we proceed.
    caption_box = _find_first(page, [_VERIFY_CAPTION_SCREEN], timeout=5.0)
    if caption_box is None:
        raise UploadActionError(
            "Step 7 GUARD FAILED: caption textarea not visible — UI is desynchronized"
        )
    logger.info("[upload] caption screen confirmed visible")
    behavior.idle(*_AFTER_NEXT_PAUSE_S)

    # Step 8: Write caption
    logger.info("[upload] step 8: Write Caption")
    if caption:
        if not safe_coordinate_click(page, _VERIFY_CAPTION_SCREEN, timeout=5):
            raise UploadActionError("Caption editor click failed")
        behavior.idle(0.3, 0.7)
        page.actions.type(caption)
        behavior.read_pause(content_length=len(caption))

    # Step 9: Optional location
    if location:
        logger.info("[upload] step 9: Add Location (%r)", location)
        if safe_coordinate_click(page, 'css:input[placeholder="Add location"]', timeout=6):
            behavior.idle(0.3, 0.6)
            page.actions.type(location)
            behavior.idle(1.5, 2.5)
            try:
                first_suggestion = page.ele('css:div[role="listbox"] button', timeout=3)
                if first_suggestion:
                    first_suggestion.scroll.to_see(center=True)
                    page.wait(0.3)
                    x, y = first_suggestion.rect.midpoint
                    page.actions.move_to((x, y)).click()
                    behavior.idle(0.5, 1.0)
            except Exception as exc:
                logger.debug("[upload] location suggestion click failed (%s)", exc)
        else:
            logger.info("[upload] location input not found — skipping")
    else:
        logger.info("[upload] step 9: no location — skipping")

    # Step 10: Optional advanced settings
    if hide_likes or disable_comments:
        logger.info("[upload] step 10: Advanced settings")
        if safe_coordinate_click(page, 't:span@text()=Advanced settings', timeout=4):
            behavior.idle(0.8, 1.4)
            if hide_likes:
                try:
                    switches = page.eles('css:input[role="switch"]', timeout=3)
                    if switches and len(switches) >= 1:
                        sw = switches[0]
                        sw.scroll.to_see(center=True)
                        page.wait(0.3)
                        x, y = sw.rect.midpoint
                        page.actions.move_to((x, y)).click()
                        logger.info("[upload] toggled hide likes")
                        behavior.idle(0.4, 0.8)
                except Exception as exc:
                    logger.debug("[upload] hide_likes toggle failed: %s", exc)
            if disable_comments:
                try:
                    switches = page.eles('css:input[role="switch"]', timeout=3)
                    if switches and len(switches) >= 2:
                        sw = switches[1]
                        sw.scroll.to_see(center=True)
                        page.wait(0.3)
                        x, y = sw.rect.midpoint
                        page.actions.move_to((x, y)).click()
                        logger.info("[upload] toggled disable comments")
                        behavior.idle(0.4, 0.8)
                except Exception as exc:
                    logger.debug("[upload] disable_comments toggle failed: %s", exc)
        else:
            logger.info("[upload] 'Advanced settings' not found — skipping")
    else:
        logger.info("[upload] step 10: no advanced settings — skipping")

    # Step 11: Share
    logger.info("[upload] step 11: Click Share (verified)")
    page.wait(2.0)  # Mandatory pre-wait for React state

    try:
        behavior.dismiss_interruptions()
    except Exception as exc:
        logger.debug("[upload] pre-Share dismiss raised (%s); continuing", exc)

    # Verification: the post/reel shared confirmation or the Share
    # button disappearing (which means IG accepted and is processing).
    _VERIFY_SHARE_DONE = (
        'xpath://*[contains(text(),"has been shared") or '
        'contains(text(),"Post shared") or '
        'contains(text(),"Reel shared")]'
    )
    try:
        safe_coordinate_click(
            page,
            't:div@text()=Share',
            timeout=15,
            verify_locator='t:div@text()=Share',
            verify_disappear=True,  # Share button should vanish on success
            max_retries=3,
            verify_timeout=10.0,
        )
    except ClickVerificationError:
        # Fallback: try button variant with same verification.
        try:
            safe_coordinate_click(
                page,
                'xpath://button[normalize-space()="Share"]',
                timeout=5,
                verify_locator='xpath://button[normalize-space()="Share"]',
                verify_disappear=True,
                max_retries=2,
                verify_timeout=10.0,
            )
        except ClickVerificationError:
            raise UploadActionError(
                "Step 11 FAILED: Share button clicked but never disappeared "
                "— post was NOT submitted"
            )

    logger.info("[upload] Share clicked and verified")

    # wait to finish and close
    marker = _step("wait for completion",
                   lambda: _wait_for_completion(browser, upload_timeout_s))

    _step("click Done",
          lambda: _click_done(browser, behavior))

    logger.info("[upload] success file=%s", abs_path)
    return {
        "action": "upload",
        "status": "success",
        "file": file_path,
        "caption_length": len(caption),
        "location": location or None,
        "completion_marker": marker,
    }


# smoke test
_SMOKE_TEST_PROXY: str = "8d1f77cde74f6dffffea__cr.us:80fe1a46ee235b27@gw.dataimpulse.com:823"

_SMOKE_TEST_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/132.0.0.0 Safari/537.36"
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
    print("  Instagram Worker - Upload v3 Smoke Test")
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
