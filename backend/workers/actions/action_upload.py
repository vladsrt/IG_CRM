"""
Upload Action — v3 (native OS-dialog interception)
--------------------------------------------------
Drives Instagram's web "Create → Post / Reel" flow against an already
authenticated ``InstagramBrowser`` and uploads the file at
``args["file_path"]``.

Why a rewrite?
~~~~~~~~~~~~~~
The previous implementation injected the file via the hidden
``<input type="file">`` element. IG started gating that input behind a
React state that only un-mocks once the visible "Select from computer"
button has been clicked AND a real ``change`` event has fired through
the OS file dialog. Hidden-input injection no longer works — the form
silently sits at the file-picker step until the upload timeout expires.

The fix: pre-arm DrissionPage's ``page.set.upload_files(path)`` BEFORE
the click. DrissionPage intercepts the OS native file dialog at the
CDP level, so the click that would normally pop a Finder/Explorer
window instead fires a synthetic ``change`` on the real input and IG's
state machine moves forward.

Every visible-element interaction is still routed through
:class:`HumanBehaviorEngine` so the session emits Bezier mouse
trajectories, variable keystroke timing, smooth scrolls, and
hesitation pauses.

CRITICAL-4 — file_path is validated against ``settings.MEDIA_ROOT`` via
:func:`workers.core.safety.resolve_within_media_root` before we hand it
to DrissionPage. An operator-supplied path that escapes the media root
(or doesn't exist, or isn't a regular file) is rejected with
``UploadActionError`` before any DOM interaction.

Public API
~~~~~~~~~~
``execute_upload(browser, args) -> dict`` — invoked by ``TaskExecutor``.
The browser is owned by the executor; this module never instantiates
one in production and never closes it.

Supported ``args``
~~~~~~~~~~~~~~~~~~
* ``file_path``         (required, str)  — absolute path to image or video
                                           UNDER ``settings.MEDIA_ROOT``
* ``caption``           (optional, str)  — caption text. Defaults to ``""``.
* ``upload_timeout_s``  (optional, int)  — overall ceiling on the share→done wait
                                           (default 180s; raise for big videos)

Note: ``location``, ``alt_text``, ``hide_likes``, ``disable_comments``
have been removed in this rewrite — IG's new flow no longer exposes
those controls inline. Add them back when/if they reappear.
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
from workers.core.behavior import HumanBehaviorEngine
from workers.core.browser_core import InstagramBrowser
from workers.core.safety import UnsafePathError, resolve_within_media_root

logger = logging.getLogger(__name__)


# ── URLs & tunables ─────────────────────────────────────────────────────
_HOME_URL: str = "https://www.instagram.com/"
_DEFAULT_STEP_TIMEOUT_S: float = 20.0
_DEFAULT_UPLOAD_TIMEOUT_S: float = 180.0
_FILE_UPLOAD_PROCESSING_S: tuple[float, float] = (4.0, 8.0)
_AFTER_NEXT_PAUSE_S: tuple[float, float] = (1.4, 2.6)


# ── Errors ──────────────────────────────────────────────────────────────
class UploadActionError(RuntimeError):
    """Raised when a step in the upload flow cannot complete."""


# ── Helpers ─────────────────────────────────────────────────────────────
def _find_first(
    page: Any,
    selectors: Iterable[str],
    *,
    timeout: float = _DEFAULT_STEP_TIMEOUT_S,
) -> Any | None:
    """Return the first selector that resolves to a real element, or ``None``.

    The overall ``timeout`` budget is split across the candidate
    selectors so a missing locator never burns the full window on its
    own. Per-selector exceptions are caught and logged at debug — the
    iteration continues.
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


def _humanized_click_first(
    page: Any,
    behavior: HumanBehaviorEngine,
    selectors: Iterable[str],
    *,
    timeout: float = _DEFAULT_STEP_TIMEOUT_S,
    label: str,
    safe: bool = True,
) -> Any:
    """Find the first matching selector and click it through the behavior engine.

    Defaults to ``safe=True`` because every commit-style button in this
    new flow (Next, Share, Done) is liable to render below the fold on
    small viewports. The caller can pass ``safe=False`` for non-commit
    elements (e.g., the crop dropdown trigger).
    """
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
    """Wrap a step so failures carry their step name in the error chain."""
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
    """Pre-arm DrissionPage's native file-dialog interception.

    The next click that would pop an OS file dialog will be answered
    with ``abs_path`` instead. Failure here is fatal — without this
    arming, the visible "Select from computer" button will pop a
    real OS dialog and freeze the session.
    """
    try:
        browser.page.set.upload_files(abs_path)
    except Exception as exc:
        raise UploadActionError(
            f"page.set.upload_files({abs_path!r}) failed: {exc}"
        ) from exc
    logger.info("[upload] file-dialog interception armed for %s", abs_path)


# ── Step implementations ────────────────────────────────────────────────
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

    # Skim the feed briefly before doing anything (real users don't load
    # IG and instantly hit Create).
    behavior.read_pause(content_length=None)
    behavior.micro_scroll()


def _open_create_dialog_from_home(
    browser: InstagramBrowser, behavior: HumanBehaviorEngine
) -> bool:
    """Click the home-page New-post button and verify the create modal.

    Returns True iff the "Create new post" heading is visible after
    the click. Returns False (instead of raising) so the caller can
    decide whether to try the profile-page fallback.
    """
    try:
        _humanized_click_first(
            browser.page,
            behavior,
            ['css:svg[aria-label="New post"]'],
            label="New post (home page)",
            timeout=8.0,
            safe=False,
        )
    except UploadActionError as exc:
        logger.warning("[upload] home-page New-post button not clickable: %s", exc)
        return False

    behavior.idle(0.6, 1.4)

    # Some accounts get a Post / Reel / Story submenu; click "Post" if it
    # appears. This is an optional step — absence is fine.
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
            behavior.click(submenu_post)
            behavior.idle(0.5, 1.1)
        except Exception as exc:
            logger.debug("[upload] submenu Post click failed (%s); ignoring", exc)

    return _wait_for_create_modal(browser, timeout_s=8.0)


def _wait_for_create_modal(
    browser: InstagramBrowser, *, timeout_s: float = 8.0
) -> bool:
    """Verify the 'Create new post' modal is open. Returns ``bool``, never raises."""
    deadline = time.monotonic() + timeout_s
    selectors: list[str] = [
        'xpath://div[@role="heading" and @aria-level="1" and contains(.,"Create new post")]',
        'xpath://*[@aria-level="1" and @role="heading" and contains(.,"Create new")]',
        'xpath://h1[contains(.,"Create new post")]',
        'css:button:has-text("Select from computer")',
        'xpath://button[normalize-space()="Select from computer"]',
    ]
    while time.monotonic() < deadline:
        if _find_first(browser.page, selectors, timeout=1.5) is not None:
            return True
    return False


def _open_create_dialog_via_profile(
    browser: InstagramBrowser, behavior: HumanBehaviorEngine
) -> None:
    """Fallback: navigate to the user's own profile and trigger the Create flow.

    Used when the home-page ``svg[aria-label="New post"]`` is missing —
    happens on accounts where IG has reshuffled the left-rail icons or
    on first-load before client-side hydration finishes.

    The flow is:

        1. Click profile avatar in left rail (or directly hit the
           profile URL — same effect, fewer flakes).
        2. Click the Reels tab on the profile.
        3. Click the empty-state "Share your first photo" /
           "Create" button that IG renders inside the empty Reels grid.

    Raises ``UploadActionError`` if the create modal still doesn't open.
    """
    logger.info("[upload] falling back via own-profile Create flow")

    # Going straight to /accounts/edit/'s redirect target works, but the
    # cleanest URL is the navigation to one's own profile via the avatar
    # nav. We can't compute it without a username; navigate the page
    # there via the avatar locator.
    try:
        _humanized_click_first(
            browser.page,
            behavior,
            [
                'css:a[href^="/"][role="link"] img[alt$=" profile picture"]',
                'css:nav a[href^="/"][role="link"]:has(img)',
                'xpath://nav//a[@role="link" and starts-with(@href,"/")][.//img]',
            ],
            label="Own-profile nav avatar",
            timeout=6.0,
            safe=False,
        )
    except UploadActionError as exc:
        raise UploadActionError(
            f"fallback: could not click profile avatar — {exc}"
        ) from exc

    behavior.idle(1.5, 2.6)

    # Click the Reels tab on the profile page.
    try:
        _humanized_click_first(
            browser.page,
            behavior,
            ['css:svg[aria-label="Reels"]'],
            label="Profile Reels tab",
            timeout=6.0,
            safe=True,
        )
    except UploadActionError as exc:
        raise UploadActionError(
            f"fallback: Reels tab not found on profile — {exc}"
        ) from exc

    behavior.idle(1.0, 2.2)

    # Click the empty-state "Share your first photo" / "Create" button.
    create_btn = _find_first(
        browser.page,
        [
            'xpath://div[@role="button"][contains(.,"Share your first photo")]',
            'xpath://div[@role="button"][contains(.,"Share your first reel")]',
            'xpath://*[@role="button" and contains(.,"Create")]',
            'xpath://button[normalize-space()="Create"]',
        ],
        timeout=8.0,
    )
    if create_btn is None:
        raise UploadActionError(
            "fallback: no empty-state Create / 'Share your first photo' button found"
        )
    try:
        behavior.safe_click_button(create_btn)
    except Exception as exc:
        raise UploadActionError(
            f"fallback: empty-state Create click failed — {exc}"
        ) from exc

    behavior.idle(1.0, 2.0)

    if not _wait_for_create_modal(browser, timeout_s=8.0):
        raise UploadActionError(
            "fallback: 'Create new post' modal did not appear after profile-Reels create click"
        )


def _click_select_from_computer(
    browser: InstagramBrowser, behavior: HumanBehaviorEngine
) -> None:
    """Click the visible 'Select from computer' button.

    The file-dialog interception MUST already be armed (see
    :func:`_arm_file_upload`). Clicking this button is what fires the
    OS native file dialog DrissionPage will intercept.
    """
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
    """Open the crop dropdown and pick 'Original' aspect ratio.

    This step is robust to absence — some images already arrive at a
    supported AR and IG skips the crop UI. We log and continue rather
    than fail the whole upload if the dropdown isn't on screen.
    """
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
    """Click Share, scrolling it into view first."""
    _humanized_click_first(
        browser.page,
        behavior,
        [
            'xpath://div[@role="button" and normalize-space()="Share"]',
            'xpath://button[normalize-space()="Share"]',
            'css:div[role="button"]:has-text("Share")',
            'text:Share',
        ],
        label="Share button",
        timeout=_DEFAULT_STEP_TIMEOUT_S,
        safe=True,
    )


def _wait_for_completion(browser: InstagramBrowser, timeout_s: float) -> str:
    """Block until IG confirms the post landed. Returns the matched marker.

    The new flow uses an ``<h3>`` toast for both reels and posts. We
    accept either the explicit reel/post copy or the universal "shared"
    fragment, and treat a "Done" button appearing in the same dialog as
    a positive completion signal.
    """
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
    """Best-effort dismiss of the completion modal."""
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


# ── Public entrypoint ───────────────────────────────────────────────────
def execute_upload(
    browser: InstagramBrowser, args: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Drive Instagram's Create-Post flow end-to-end with humanized inputs
    and native OS-dialog file interception.

    The browser is owned by the caller (``TaskExecutor``); this function
    never instantiates a new one and never calls ``browser.close()``.
    Step failures propagate as ``UploadActionError`` so the executor
    can mark the parent Task FAILED with a useful message.
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
    upload_timeout_s = float(args.get("upload_timeout_s", _DEFAULT_UPLOAD_TIMEOUT_S))

    logger.info(
        "[upload] starting file=%s caption_len=%d", abs_path, len(caption),
    )

    # One engine per upload session — holds the cursor position across steps.
    behavior = HumanBehaviorEngine(browser.page)

    # ── Phase 1: navigate + open the create modal ──────────────────────
    _step("navigate to home feed",
          lambda: _navigate_home(browser, behavior))

    used_fallback = False
    if not _open_create_dialog_from_home(browser, behavior):
        logger.warning(
            "[upload] primary 'New post' flow failed; trying profile-Reels fallback"
        )
        used_fallback = True
        _step("open create modal via profile fallback",
              lambda: _open_create_dialog_via_profile(browser, behavior))

    # ── Phase 2: arm the file dialog, click Select, wait for processing ─
    _step("arm DrissionPage native file-dialog interception",
          lambda: _arm_file_upload(browser, abs_path))

    _step("click 'Select from computer'",
          lambda: _click_select_from_computer(browser, behavior))

    # IG processes the upload client-side before the crop UI appears.
    behavior.idle(*_FILE_UPLOAD_PROCESSING_S)

    # ── Phase 3: crop step (set Original aspect ratio) ─────────────────
    _step("set crop to Original",
          lambda: _set_crop_to_original(browser, behavior))

    _step("click Next (post-crop)",
          lambda: _click_next(browser, behavior, label="Next button (crop step)"))

    # ── Phase 4: optional cover-photo / audio panel ────────────────────
    # IG inserts a second 'Next' page for some media types (cover image
    # for reels, audio toggle for posts with sound). Click Next if a
    # second Next button is still present; if it's not, we're already
    # on the caption step.
    second_next = _find_first(
        browser.page,
        [
            'xpath://div[@role="button" and normalize-space()="Next"]',
            'xpath://button[normalize-space()="Next"]',
        ],
        timeout=4.0,
    )
    if second_next is not None:
        _step("click Next (cover/audio step)",
              lambda: _click_next(browser, behavior, label="Next button (cover/audio step)"))
    else:
        logger.info("[upload] no second Next button — skipping cover/audio step")

    # ── Phase 5: caption ───────────────────────────────────────────────
    _step("write caption",
          lambda: _write_caption(browser, behavior, caption))

    # ── Phase 6: share + wait for confirmation + dismiss ───────────────
    _step("click Share",
          lambda: _click_share(browser, behavior))

    marker = _step("wait for completion",
                   lambda: _wait_for_completion(browser, upload_timeout_s))

    _step("click Done",
          lambda: _click_done(browser, behavior))

    logger.info("[upload] success file=%s used_fallback=%s", abs_path, used_fallback)
    return {
        "action": "upload",
        "status": "success",
        "file": file_path,
        "caption_length": len(caption),
        "used_fallback_flow": used_fallback,
        "completion_marker": marker,
    }


# ── Standalone smoke-test (not used in production) ──────────────────────
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
