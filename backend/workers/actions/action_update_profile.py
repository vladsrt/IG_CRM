"""
Update Profile Action
---------------------
Drives Instagram's web profile-edit + privacy-settings flow against an
already authenticated ``InstagramBrowser``. Updates one or more of:

    * Bio text                    (``args["bio"]``)
    * Avatar / profile picture    (``args["avatar_path"]``)
    * Account privacy             (``args["is_private"]``)

Every visible-element interaction is routed through
:class:`HumanBehaviorEngine` (Bezier mouse, variable typing cadence,
reading pauses). The only bypass is the hidden ``<input type="file">`` for
the avatar — same rationale as ``action_upload``: a hidden element has
no usable bounding rect, and a file injection is a programmatic event,
not a user gesture.

CRITICAL-4 — ``avatar_path`` is validated against ``settings.MEDIA_ROOT``
via :func:`workers.core.safety.resolve_within_media_root` before we hand
it to DrissionPage. An operator-supplied path that escapes the media
root (or doesn't exist, or isn't a regular file) is rejected with
``ProfileActionError`` before any DOM interaction.

Public API
~~~~~~~~~~
``execute_update_profile(browser, args) -> dict`` — invoked by
``TaskExecutor``. The browser is owned by the executor; this module
never instantiates one in production and never closes it. All step
failures propagate as :class:`ProfileActionError` so the executor can
mark the parent Task ``FAILED``.

Note on the website-link field
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Instagram removed the editable "Website" field from the web profile-edit
form (DOM reconnaissance confirmed). This handler intentionally does NOT
attempt to set it, even if the LLM produces a `website` arg.
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


# ── URLs & tunables ─────────────────────────────────────────────────────
_EDIT_URL: str = "https://www.instagram.com/accounts/edit/"
_PRIVACY_URL: str = "https://www.instagram.com/accounts/who_can_see_your_content/"

_DEFAULT_STEP_TIMEOUT_S: float = 20.0
_AVATAR_PROCESS_WAIT_RANGE_S: tuple[float, float] = (3.0, 4.5)
_SAVE_MARKER_TIMEOUT_S: float = 30.0
_SAVE_POLL_INTERVAL_S: float = 1.5
_AVATAR_CHANGE_DIALOG_TIMEOUT_S: float = 12.0
_AVATAR_TOAST_POLL_INTERVAL_S: float = 0.5


# ── Errors ──────────────────────────────────────────────────────────────
class ProfileActionError(RuntimeError):
    """Raised when a step in the update-profile flow cannot complete."""


# ── Selector helpers (mirror action_upload's pattern; see design notes) ─
def _find_first(
    page: Any,
    selectors: Iterable[str],
    *,
    timeout: float = _DEFAULT_STEP_TIMEOUT_S,
) -> Any | None:
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
    """Locate the first matching element and click it via the behavior engine.

    When ``safe=True`` the click is routed through
    :meth:`HumanBehaviorEngine.safe_click_button` — element is scrolled
    into view (centered) and given a 1s settle window before the click.
    Use that for any commit-style button (Submit/Save/Confirm/Switch).
    """
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
    """Wrap a step so failures carry their step name in the error chain."""
    logger.info("[update_profile] step: %s", label)
    try:
        return fn()
    except ProfileActionError:
        raise
    except Exception as exc:
        raise ProfileActionError(
            f"Step {label!r} failed: {type(exc).__name__}: {exc}"
        ) from exc


# ── Step implementations ────────────────────────────────────────────────
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
    # CRITICAL-4 — refuse anything that escapes MEDIA_ROOT or doesn't exist.
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

    # The hidden file input may live inside a modal that just opened, or
    # may already be present in the page. Either way: same locator pool
    # as action_upload, image-accept variant first.
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

    # IG processes the upload client-side before showing the new avatar in
    # the form; give it a beat that scales like a human "did it work?" wait.
    behavior.idle(*_AVATAR_PROCESS_WAIT_RANGE_S)

    # ── Change-Profile-Photo dialog blocker ─────────────────────────────
    # Current IG behaviour (as of this rollout): the file injection
    # silently applies the avatar in the background — a paragraph
    # reading "Profile photo added." pops up — but the original
    # "Change Profile Photo" dialog STAYS OPEN, sitting on top of the
    # /accounts/edit/ form and blocking pointer events on the Submit
    # button. The fix is to detect that the toast has fired (or the
    # avatar has just been processed) and click the dialog's Cancel
    # button to close it. Submit can fire only after that.
    _dismiss_change_photo_dialog(browser, behavior)


def _dismiss_change_photo_dialog(
    browser: InstagramBrowser, behavior: HumanBehaviorEngine
) -> bool:
    """Close the still-open 'Change Profile Photo' dialog after a successful
    avatar injection.

    Behaviour:

    1. Poll briefly for the "Profile photo added." paragraph as the
       positive signal that IG actually accepted the file. If we see
       it, we *must* close the dialog or the downstream Submit click
       will hit the dialog's overlay instead of the form button.
    2. With or without the toast, look for the dialog's Cancel button
       and click it. The button has appeared in two stable forms:

           * ``button:has-text("Cancel")``
           * ``button._a9--._ap36._a9_1``  (IG's class-hash variant)

       (Class hashes in IG flip every couple of months; we keep the
       hash-based selector as a last resort and lead with the text
       match.)
    3. Wait ~1s for the dialog to tear down before returning, so the
       caller's Submit click lands on a stable layout.

    Returns:
        ``True`` iff the Cancel button was successfully clicked.
        Absence of either the toast or the Cancel button is logged at
        warning level but is NOT fatal — some IG variants auto-close
        the dialog after the photo is added.
    """
    # Step 1 — poll for the "Profile photo added." paragraph as a
    # positive signal. We don't *require* it (some variants skip it),
    # but its presence is what guarantees the dialog is in the
    # blocking state we're trying to dismiss.
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

    # Step 2 — find and click Cancel on the still-open dialog.
    # All locators are TEXT- or role-based; IG's class hashes
    # (e.g. ``._a9--._ap36._a9_1``) are deliberately NOT used because
    # they rotate every few months. The DrissionPage-native ``t:``
    # form leads, with XPath fallbacks for variants where the button
    # is rendered as a ``role="button"`` div instead of a real
    # ``<button>``.
    cancel_selectors = [
        # Scoped-to-dialog first so we don't mis-click any "Cancel"
        # that might appear elsewhere on screen.
        'xpath://div[@role="dialog"]//button[normalize-space()="Cancel"]',
        'xpath://div[@role="dialog"]//*[@role="button" and normalize-space()="Cancel"]',
        # DrissionPage native text-keyed locator.
        't:button@text()=Cancel',
        # Plain text fallbacks.
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

    # Step 3 — wait 1s for the dialog to tear down.
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

    # Robust clear: DrissionPage's `.clear()` silently no-ops on the
    # contenteditable bio variant, which causes typed text to append.
    # `behavior.clear_input_field` now wipes the field via JS (the only
    # reliable path through React's controlled-input gate) and falls
    # back to keystrokes if React owns the value. Returns False iff the
    # field is still non-empty after both attempts — in that case
    # typing the new bio would *append*, which is the bug we want
    # to surface, not paper over.
    cleared = behavior.clear_input_field(bio_field, backspace_passes=2)
    if not cleared:
        raise ProfileActionError(
            "bio field could not be cleared (JS + Ctrl+A/Backspace both failed) — "
            "typing the new bio would append to existing text; aborting"
        )

    # Re-focus before typing — clear_input_field already focuses, but a
    # fresh click ensures the caret is at the start on stubborn fields.
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

    # CRITICAL: read current state via aria-checked BEFORE clicking.
    # Clicking blindly would invert the state on every run, leaving the
    # account in whichever state it didn't start in.
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

    # IG often shows a confirmation modal when flipping to private (and
    # sometimes when flipping back to public). Click through it if present.
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
    # Submit is at the bottom of /accounts/edit/ and is reliably out of
    # the viewport after a long bio is typed. `safe=True` scrolls it into
    # view (centered), waits 1s for layout to settle, then clicks. Without
    # this, DrissionPage's synthetic mousedown lands on whatever element
    # happens to be at the original coordinate and the form silently
    # never submits.
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
    """Block until IG confirms the save. Returns the matched marker selector."""
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


# ── Public entrypoint ───────────────────────────────────────────────────
def execute_update_profile(
    browser: InstagramBrowser, args: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Drive the IG profile / privacy edit flow.

    The browser is owned by the caller (``TaskExecutor``); this function
    never instantiates a new one and never calls ``browser.close()``.
    Step failures propagate as :class:`ProfileActionError` so the executor
    can mark the parent Task FAILED with a useful message.
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

    # CRITICAL-4 — pre-validate the avatar path BEFORE any browser nav so a
    # path-traversal attempt fails fast with a clear error.
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

    # Sweep any random "Turn on notifications" / "Add to home screen" /
    # cookies-banner modals before we try to interact with the form.
    # The sweep is fast (~1.5s when nothing is present) and never raises.
    try:
        behavior.dismiss_interruptions()
    except Exception as exc:
        logger.debug(
            "[update_profile] dismiss_interruptions raised unexpectedly (%s); "
            "continuing",
            exc,
        )

    # ── Bio + avatar: both live on /accounts/edit/ but persist DIFFERENTLY.
    #
    # Avatar uploads are now asynchronous on IG: the file injection
    # silently saves in the background (a "Profile photo added." toast
    # confirms it), and the Change-Profile-Photo dialog stays open.
    # We close THAT dialog with Cancel (handled inside ``_set_avatar``)
    # — clicking the global form Submit afterwards triggers an error
    # because the avatar is already persisted server-side.
    #
    # Bio updates still go through the form's Submit + "Profile saved"
    # toast. So:
    #
    #   * avatar only      → set_avatar handles its own toast+Cancel,
    #                        skip Submit/save-marker entirely.
    #   * bio only         → set_bio, Submit, wait for save marker.
    #   * bio AND avatar   → set_avatar (toast+Cancel), set_bio,
    #                        Submit, wait for save marker.
    needs_edit_page = bio is not None or avatar_path is not None
    if needs_edit_page:
        _step(
            "navigate to /accounts/edit/",
            lambda: _navigate_to_edit_page(browser, behavior),
        )
        # After landing on the edit page, run another sweep — IG often
        # injects a "Save your login info?" prompt right after a
        # navigation completes.
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
            # Avatar-only path — explicitly mark the marker as the
            # "Profile photo added." toast we already saw inside
            # ``_set_avatar``. This keeps the result dict honest
            # about what actually fired.
            logger.info(
                "[update_profile] avatar-only update — skipping form Submit "
                "(avatar already persisted via async upload)"
            )
            save_marker = "Profile photo added. (async)"

    # ── Privacy: separate page, toggle persists on click (no Submit).
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
