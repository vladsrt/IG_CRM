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

from workers.core.behavior import HumanBehaviorEngine
from workers.core.browser_core import InstagramBrowser

logger = logging.getLogger(__name__)


# ── URLs & tunables ─────────────────────────────────────────────────────
_EDIT_URL: str = "https://www.instagram.com/accounts/edit/"
_PRIVACY_URL: str = "https://www.instagram.com/accounts/who_can_see_your_content/"

_DEFAULT_STEP_TIMEOUT_S: float = 20.0
_AVATAR_PROCESS_WAIT_RANGE_S: tuple[float, float] = (3.0, 4.5)
_SAVE_MARKER_TIMEOUT_S: float = 30.0
_SAVE_POLL_INTERVAL_S: float = 1.5


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
) -> Any:
    ele = _find_first(page, selectors, timeout=timeout)
    if ele is None:
        raise ProfileActionError(
            f"Could not locate {label!r} (tried {list(selectors)})"
        )
    try:
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
    abs_path = os.path.abspath(avatar_path)
    if not os.path.isfile(abs_path):
        raise ProfileActionError(f"Avatar file does not exist: {abs_path}")

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

    # Best-effort clear of existing bio text. DrissionPage's `.clear()`
    # works on textareas/inputs; for contenteditable variants it may
    # no-op silently — in that case the new text would append. Logged
    # for diagnosis but not raised, since "append" is recoverable.
    try:
        bio_field.clear()
    except Exception as exc:
        logger.debug(
            "[update_profile] bio_field.clear() failed (%s); typing may append",
            exc,
        )

    behavior.type_into(bio_field, bio)
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
            behavior.click(confirm)
            behavior.idle(0.5, 1.0)
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
    # Final hesitation before persisting — humans pause before commit buttons.
    behavior.idle(0.5, 1.1)
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

    logger.info(
        "[update_profile] starting bio_len=%s avatar=%s is_private=%s",
        len(bio) if bio else 0,
        avatar_path,
        is_private,
    )

    behavior = HumanBehaviorEngine(browser.page)
    save_marker: Optional[str] = None

    # ── Bio + avatar: both live on /accounts/edit/ and share one Submit.
    needs_edit_page = bio is not None or avatar_path is not None
    if needs_edit_page:
        _step(
            "navigate to /accounts/edit/",
            lambda: _navigate_to_edit_page(browser, behavior),
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
