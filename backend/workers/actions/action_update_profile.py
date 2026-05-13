"""
update profile action.
edits instagram profile bio, avatar, and privacy settings.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any, Callable, Dict, Iterable, Optional

# Allow `python action_update_profile.py` from the actions/ dir for the smoke-test.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from app.core.config import settings
from workers.core.behavior import HumanBehaviorEngine
from workers.core.browser_core import InstagramBrowser
from workers.core.safety import UnsafePathError, resolve_within_media_root

logger = logging.getLogger(__name__)


# settings
_EDIT_URL: str = "https://www.instagram.com/accounts/edit/"
_PRIVACY_URL: str = "https://www.instagram.com/accounts/who_can_see_your_content/"

_DEFAULT_STEP_TIMEOUT_S: float = 20.0
_AVATAR_PROCESS_WAIT_RANGE_S: tuple[float, float] = (3.0, 4.5)
_SAVE_MARKER_TIMEOUT_S: float = 30.0
_SAVE_POLL_INTERVAL_S: float = 1.5
_AVATAR_CHANGE_DIALOG_TIMEOUT_S: float = 12.0
_AVATAR_TOAST_POLL_INTERVAL_S: float = 0.5


# errors
class ProfileActionError(RuntimeError):
    """Raised when a step in the update-profile flow cannot complete."""


# selector helpers
def _find_first(
    page: Any,
    selectors: Iterable[str],
    *,
    timeout: float = _DEFAULT_STEP_TIMEOUT_S,
) -> Any | None:
    """Return the first matching element from a list of selectors, or None."""
    selectors = list(selectors)
    if not selectors:
        return None
    per_sel = max(1.0, timeout / len(selectors))
    for sel in selectors:
        try:
            ele = page.ele(sel, timeout=per_sel)
        except Exception as exc:
            logger.debug("[update_profile] selector %r raised: %s", sel, exc)
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
    safe: bool = False,
) -> Any:
    """click the first matching element."""
    ele = _find_first(page, selectors, timeout=timeout)
    if ele is None:
        raise ProfileActionError(
            f"Could not locate {label!r} (tried {list(selectors)})"
        )
    try:
        if safe:
            behavior.safe_click_button(ele)
        else:
            behavior.click(ele)
    except Exception as exc:
        raise ProfileActionError(
            f"Found {label!r} but click failed: {exc}"
        ) from exc
    return ele


def _step(label: str, fn: Callable[[], Any]) -> Any:
    """Wrap a step to trace errors."""
    logger.info("[update_profile] step: %s", label)
    try:
        return fn()
    except ProfileActionError:
        raise
    except Exception as exc:
        raise ProfileActionError(
            f"Step {label!r} failed: {type(exc).__name__}: {exc}"
        ) from exc


# steps
def _navigate_to_edit_page(
    browser: InstagramBrowser, behavior: HumanBehaviorEngine
) -> None:
    browser.page.get(_EDIT_URL)
    landed = _find_first(
        browser.page,
        [
            "css:#pepBio",
            'xpath://*[@role="button" and normalize-space()="Submit"]',
        ],
        timeout=_DEFAULT_STEP_TIMEOUT_S,
    )
    if landed is None:
        raise ProfileActionError(
            "Edit-profile page did not render — session may be invalid"
        )
    behavior.read_pause(content_length=None)


def _set_avatar(
    browser: InstagramBrowser,
    behavior: HumanBehaviorEngine,
    avatar_path: str,
) -> None:
    # Reject anything that escapes MEDIA_ROOT or doesn't exist.
    try:
        abs_path = resolve_within_media_root(avatar_path, settings.MEDIA_ROOT)
    except UnsafePathError as exc:
        raise ProfileActionError(f"unsafe avatar_path: {exc}") from exc

    _humanized_click_first(
        browser.page,
        behavior,
        [
            'xpath://*[@role="button" and normalize-space()="Change photo"]',
            'xpath://button[normalize-space()="Change photo"]',
            "text:Change photo",
        ],
        label="Change photo button",
        timeout=_DEFAULT_STEP_TIMEOUT_S,
        safe=True,
    )
    behavior.idle(0.5, 1.2)

    # The hidden file input may live inside a modal or in the page.
    file_input = _find_first(
        browser.page,
        [
            'css:input[type="file"][accept*="image"]',
            'css:form[role="presentation"] input[type="file"]',
            'css:input[type="file"]',
        ],
        timeout=_DEFAULT_STEP_TIMEOUT_S,
    )
    if file_input is None:
        raise ProfileActionError("Avatar hidden <input type=file> not found")

    try:
        file_input.input(abs_path)
    except Exception as exc:
        raise ProfileActionError(
            f"file_input.input({abs_path!r}) failed: {exc}"
        ) from exc

    # IG processes the upload client-side before showing the new avatar
    behavior.idle(*_AVATAR_PROCESS_WAIT_RANGE_S)

    # Close the Change Profile Photo dialog so it doesn't block the submit button.
    _dismiss_change_photo_dialog(browser, behavior)


def _dismiss_change_photo_dialog(
    browser: InstagramBrowser, behavior: HumanBehaviorEngine
) -> bool:
    """close the change photo dialog."""
    # Wait for the "Profile photo added." toast as a positive signal
    toast_selectors = [
        'xpath://p[normalize-space()="Profile photo added."]',
        'xpath://*[contains(text(),"Profile photo added")]',
    ]
    deadline = time.monotonic() + _AVATAR_CHANGE_DIALOG_TIMEOUT_S
    saw_toast = False
    while time.monotonic() < deadline:
        for sel in toast_selectors:
            try:
                if browser.page.ele(sel, timeout=0.5):
                    saw_toast = True
                    break
            except Exception:
                continue
        if saw_toast:
            logger.info(
                "[update_profile] 'Profile photo added.' toast confirmed"
            )
            break
        time.sleep(_AVATAR_TOAST_POLL_INTERVAL_S)
    if not saw_toast:
        logger.warning(
            "[update_profile] 'Profile photo added.' toast not seen within %ds; "
            "still attempting to dismiss the change-photo dialog",
            int(_AVATAR_CHANGE_DIALOG_TIMEOUT_S),
        )

    # Find and click Cancel on the still-open dialog
    cancel_selectors = [
        'xpath://div[@role="dialog"]//button[normalize-space()="Cancel"]',
        'xpath://div[@role="dialog"]//*[@role="button" and normalize-space()="Cancel"]',
        't:button@text()=Cancel',
        'xpath://button[normalize-space()="Cancel"]',
        'xpath://*[@role="button" and normalize-space()="Cancel"]',
    ]
    cancel_btn = _find_first(browser.page, cancel_selectors, timeout=4.0)
    if cancel_btn is None:
        logger.warning(
            "[update_profile] Change-Profile-Photo dialog Cancel button not "
            "found; the dialog may have auto-closed — proceeding to Submit"
        )
        return False

    logger.info("[update_profile] clicking Cancel to close change-photo dialog")
    try:
        behavior.safe_click_button(cancel_btn)
    except Exception as exc:
        logger.warning(
            "[update_profile] safe_click_button on Cancel failed (%s); "
            "trying plain click",
            exc,
        )
        try:
            cancel_btn.click()
        except Exception as exc2:
            logger.error(
                "[update_profile] Cancel click failed entirely (%s); the "
                "dialog will likely block Submit",
                exc2,
            )
            return False

    # Wait for the dialog to tear down
    time.sleep(1.0)
    return True


def _set_bio(
    browser: InstagramBrowser,
    behavior: HumanBehaviorEngine,
    bio: str,
) -> None:
    bio_field = _find_first(
        browser.page,
        [
            "css:#pepBio",
            'css:textarea[id="pepBio"]',
            'css:textarea[aria-label="Bio"]',
        ],
        timeout=_DEFAULT_STEP_TIMEOUT_S,
    )
    if bio_field is None:
        raise ProfileActionError("Bio field (#pepBio) not found")

    # Clear the field with JS and fallback keystrokes
    cleared = behavior.clear_input_field(bio_field, backspace_passes=2)
    if not cleared:
        raise ProfileActionError(
            "bio field could not be cleared (JS + Ctrl+A/Backspace both failed) — "
            "typing the new bio would append to existing text; aborting"
        )

    # Type the new bio
    behavior.type_into(bio_field, bio, focus_first=False)
    behavior.read_pause(content_length=len(bio))


def _set_privacy(
    browser: InstagramBrowser,
    behavior: HumanBehaviorEngine,
    is_private: bool,
) -> None:
    browser.page.get(_PRIVACY_URL)
    behavior.read_pause(content_length=None)

    toggle = _find_first(
        browser.page,
        [
            'css:input[role="switch"][aria-label="Private account"]',
            'xpath://input[@role="switch" and @aria-label="Private account"]',
            'css:[role="switch"][aria-label="Private account"]',
            'xpath://*[@role="switch" and @aria-label="Private account"]',
        ],
        timeout=_DEFAULT_STEP_TIMEOUT_S,
    )
    if toggle is None:
        raise ProfileActionError(
            "Private-account toggle not found on privacy page"
        )

    # Read current state via aria-checked before clicking
    try:
        current_attr = toggle.attr("aria-checked")
    except Exception as exc:
        raise ProfileActionError(
            f"Could not read aria-checked on privacy toggle: {exc}"
        ) from exc

    currently_private = (current_attr or "").strip().lower() == "true"
    desired_private = bool(is_private)

    if currently_private == desired_private:
        logger.info(
            "[update_profile] privacy already %s — skipping toggle click",
            "private" if currently_private else "public",
        )
        return

    behavior.click(toggle)
    behavior.idle(0.6, 1.3)

    # Click through privacy confirmation modal if present
    confirm = _find_first(
        browser.page,
        [
            'xpath://button[normalize-space()="Switch to private account"]',
            'xpath://button[normalize-space()="Switch to public account"]',
            'xpath://*[@role="button" and starts-with(normalize-space(),"Switch to")]',
            'xpath://button[normalize-space()="Confirm"]',
        ],
        timeout=4.0,
    )
    if confirm is not None:
        try:
            behavior.safe_click_button(confirm)
        except Exception as exc:
            logger.debug(
                "[update_profile] privacy confirmation click failed (%s); ignoring",
                exc,
            )

    logger.info(
        "[update_profile] privacy toggled %s → %s",
        "private" if currently_private else "public",
        "private" if desired_private else "public",
    )


def _click_submit(
    browser: InstagramBrowser, behavior: HumanBehaviorEngine
) -> None:
    # Submit is often below the viewport; safe=True scrolls it into view
    _humanized_click_first(
        browser.page,
        behavior,
        [
            'xpath://div[@role="button" and normalize-space()="Submit"]',
            'xpath://div[@role="button" and text()="Submit"]',
            'xpath://button[normalize-space()="Submit"]',
            'xpath://*[@role="button" and normalize-space()="Save"]',
        ],
        label="Submit button",
        timeout=_DEFAULT_STEP_TIMEOUT_S,
        safe=True,
    )


def _wait_for_save_marker(
    browser: InstagramBrowser, timeout_s: float = _SAVE_MARKER_TIMEOUT_S
) -> str:
    """wait for the save confirmation toast."""
    deadline = time.monotonic() + timeout_s
    confirm_selectors = [
        'xpath://*[contains(text(),"Profile saved.")]',
        'xpath://*[contains(text(),"Settings saved.")]',
        'xpath://*[contains(text(),"Profile saved")]',
        'xpath://*[contains(text(),"Settings saved")]',
        'xpath://*[contains(text(),"Changes saved")]',
    ]
    error_selectors = [
        'xpath://*[contains(text(),"Something went wrong")]',
        'xpath://*[contains(text(),"could not be saved")]',
        'xpath://*[contains(text(),"Try again")]',
    ]

    while time.monotonic() < deadline:
        for sel in confirm_selectors:
            try:
                ele = browser.page.ele(sel, timeout=1)
            except Exception:
                continue
            if ele:
                logger.info(
                    "[update_profile] save marker matched: %s", sel
                )
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
                raise ProfileActionError(
                    f"Instagram reported a save error: {txt or sel}"
                )

        time.sleep(_SAVE_POLL_INTERVAL_S)

    raise ProfileActionError(
        f"Profile save did not confirm within {timeout_s:.0f}s — no marker found"
    )


# main entry point
def execute_update_profile(
    browser: InstagramBrowser, args: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    runs the profile update process.
    returns dict with results.
    """
    args = args or {}

    bio_raw = args.get("bio")
    bio: Optional[str] = (
        str(bio_raw) if bio_raw not in (None, "") else None
    )

    avatar_raw = args.get("avatar_path")
    avatar_path: Optional[str] = (
        str(avatar_raw) if avatar_raw not in (None, "") else None
    )

    is_private_raw = args.get("is_private")
    is_private: Optional[bool] = (
        bool(is_private_raw) if is_private_raw is not None else None
    )

    if bio is None and avatar_path is None and is_private is None:
        raise ProfileActionError(
            "update_profile requires at least one of: bio, avatar_path, is_private"
        )

    # Validate the path before any browser nav
    if avatar_path is not None:
        try:
            avatar_path = resolve_within_media_root(avatar_path, settings.MEDIA_ROOT)
        except UnsafePathError as exc:
            raise ProfileActionError(f"unsafe avatar_path: {exc}") from exc

    logger.info(
        "[update_profile] starting bio_len=%s avatar=%s is_private=%s",
        len(bio) if bio else 0,
        avatar_path,
        is_private,
    )

    behavior = HumanBehaviorEngine(browser.page)
    save_marker: Optional[str] = None

    # Sweep modals before interacting with the form
    try:
        behavior.dismiss_interruptions()
    except Exception as exc:
        logger.debug(
            "[update_profile] dismiss_interruptions raised unexpectedly (%s); "
            "continuing",
            exc,
        )

    # Handle avatar and bio updates on the edit page
    needs_edit_page = bio is not None or avatar_path is not None
    if needs_edit_page:
        _step(
            "navigate to /accounts/edit/",
            lambda: _navigate_to_edit_page(browser, behavior),
        )
        # Post-nav sweep
        try:
            behavior.dismiss_interruptions()
        except Exception as exc:
            logger.debug(
                "[update_profile] post-nav dismiss_interruptions raised (%s)", exc
            )
        if avatar_path is not None:
            _step(
                "set avatar",
                lambda: _set_avatar(browser, behavior, avatar_path),
            )
        if bio is not None:
            _step(
                "set bio",
                lambda: _set_bio(browser, behavior, bio),
            )
            _step("click Submit", lambda: _click_submit(browser, behavior))
            save_marker = _step(
                "wait for save marker",
                lambda: _wait_for_save_marker(browser),
            )
        else:
            # Avatar already saved asynchronously
            logger.info(
                "[update_profile] avatar-only update — skipping form Submit "
                "(avatar already persisted via async upload)"
            )
            save_marker = "Profile photo added. (async)"

    # Privacy is on a separate page
    if is_private is not None:
        _step(
            "set privacy toggle",
            lambda: _set_privacy(browser, behavior, is_private),
        )

    logger.info(
        "[update_profile] success bio=%s avatar=%s is_private=%s marker=%s",
        bio is not None, avatar_path is not None, is_private, save_marker,
    )
    return {
        "action": "update_profile",
        "status": "success",
        "bio_set": bio is not None,
        "bio_length": len(bio) if bio else 0,
        "avatar_set": avatar_path is not None,
        "avatar_path": avatar_path,
        "privacy_set": is_private is not None,
        "is_private": is_private,
        "save_marker": save_marker,
    }
