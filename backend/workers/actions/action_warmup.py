"""
Warmup Action — v2 ("Warmup 2.0")
---------------------------------
Drives a humanized, time-bounded Instagram session against an
already-authenticated ``InstagramBrowser``. The goal is *not* to do
anything observable on the account; it is to make the account look
alive — spending realistic time on the feed, occasionally watching
Reels, dipping into comment threads, visiting profiles, liking the
odd post. No two sessions look the same.

Design — the time-based event loop
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The whole session is one ``while time.monotonic() < end_time`` loop.
Each tick the script picks a single high-level action by weighted
random choice — by default:

    * scroll feed only       60%
    * watch Reels for a bit  15%
    * open comment thread    15%
    * visit a profile        10%

The picked action runs, returns, and we go back to the loop. Most
actions also include their own internal smooth-scrolls and humanized
pauses, so the session naturally varies in rhythm.

Every visible-element interaction is routed through
:class:`HumanBehaviorEngine` — Bezier mouse trajectories, JS-driven
smooth scroll, hover-then-click, swallowed locator errors. Missing
elements never crash the session; they just bias the next dice roll.

Public API
~~~~~~~~~~
``execute_warmup(browser, args)`` — invoked by ``TaskExecutor``. The
browser is owned by the executor; this module never instantiates one in
production. The action never swallows fatal exceptions; per-tick failures
are caught (so one bad locator doesn't end a 15-minute session) but
configuration errors and broken sessions propagate.

Tunable args (all optional)
~~~~~~~~~~~~~~~~~~~~~~~~~~~
* ``feed_url`` (str)              — URL to start on. Default IG home.
* ``page_load_wait_s`` (float)    — Initial wait after navigation.
* ``duration_minutes`` (float)    — Total session ceiling. Default 15.
* ``action_weights`` (dict)       — Override the default weighted-choice
                                    distribution (see ``DEFAULT_WEIGHTS``).
"""

from __future__ import annotations

import logging
import os
import random
import sys
import time
from typing import Any, Callable, Dict, List, Optional

# Allow `python action_warmup.py` from the actions/ dir for the smoke-test.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from workers.core.behavior import (
    HumanBehaviorEngine,
    ClickVerificationError,
    dismiss_instagram_modals,
    safe_coordinate_click,
)
from workers.core.browser_core import InstagramBrowser

logger = logging.getLogger(__name__)


# ── Defaults ────────────────────────────────────────────────────────────
DEFAULT_FEED_URL: str = "https://www.instagram.com/"
DEFAULT_PAGE_LOAD_WAIT_S: float = 5.0
DEFAULT_DURATION_MINUTES: float = 15.0

# Weights are picked by ``random.choices`` — relative magnitudes only,
# they don't have to sum to 1.0. Re-tune freely via the ``action_weights``
# arg without code changes.
DEFAULT_WEIGHTS: Dict[str, float] = {
    "scroll_feed": 60.0,
    "watch_reels": 15.0,
    "open_comments": 15.0,
    "visit_profile": 10.0,
}

# ── Locators (curated against current IG web DOM as of 2026-Q2) ────────
# Single source of truth — bumping a selector here updates every action.
PROFILE_LINK_LOCATOR = 'css:a[role="link"][href^="/"] span[dir="auto"]'
PROFILE_FIRST_POST_LOCATOR = 'css:a[href*="/p/"], a[href*="/reel/"]'
COMMENT_ICON_LOCATOR = 'css:svg[aria-label="Comment"][height="24"]'
POST_LIKE_ICON_LOCATOR = 'css:svg[aria-label="Like"][height="24"]'
CLOSE_ICON_LOCATOR = 'css:svg[aria-label="Close"]'
REELS_TAB_LOCATOR = 'css:svg[aria-label="Reels"]'
NEXT_REEL_LOCATOR = 'css:div[aria-label="Navigate to next Reel"] svg'
# Comment hearts are smaller (12px or 16px) and we MUST NOT toggle a
# heart that is already in the "Unlike" state — that would un-like a
# real user's comment, which is observable and bad.
COMMENT_LIKE_LOCATORS: List[str] = [
    'css:svg[aria-label="Like"][height="12"]',
    'css:svg[aria-label="Like"][height="16"]',
]

# ── Parent-button locators ─────────────────────────────────────────────
# CRITICAL: in Reels (and on the feed too, increasingly) the SVG icons
# themselves either have ``pointer-events: none`` or are positioned in a
# container that doesn't receive clicks. A coordinate click on the SVG
# passes THROUGH to whatever element is underneath — for Reels that's
# the video, so the click toggles play/pause instead of liking. The
# React click handler lives on the wrapping ``<div role="button">``
# (or sometimes ``<button>`` / ``<a>``). We walk up from the SVG to
# the nearest interactive ancestor and click THAT.
#
# Each list is searched in order — most specific (height-pinned) first,
# generic fallbacks after, in case Reels renders the icon at a
# different size or via a slightly different DOM path.
POST_LIKE_BUTTON_LOCATORS: List[str] = [
    'xpath://*[(@role="button" or self::button) '
    'and .//svg[@aria-label="Like" and @height="24"]]',
    'xpath://*[(@role="button" or self::button) '
    'and .//svg[@aria-label="Like"]]',
    'xpath://*[@role="button" and @aria-label="Like"]',
]
COMMENT_BUTTON_LOCATORS: List[str] = [
    'xpath://*[(@role="button" or self::button) '
    'and .//svg[@aria-label="Comment" and @height="24"]]',
    'xpath://*[(@role="button" or self::button) '
    'and .//svg[@aria-label="Comment"]]',
    'xpath://*[@role="button" and @aria-label="Comment"]',
]
COMMENT_LIKE_BUTTON_LOCATORS: List[str] = [
    'xpath://*[(@role="button" or self::button) '
    'and .//svg[@aria-label="Like" and (@height="12" or @height="16")]]',
    'xpath://ul//*[(@role="button" or self::button) '
    'and .//svg[@aria-label="Like"]]',
    'xpath://div[@role="dialog"]//*[(@role="button" or self::button) '
    'and .//svg[@aria-label="Like"]]',
]

# Per-tick budgets — the loop stops cleanly between ticks so a long Reel
# watch can't blow the overall duration_minutes budget by more than ~30s.
_REEL_WATCH_S_RANGE: tuple[float, float] = (5.0, 30.0)
_REELS_PER_VISIT_RANGE: tuple[int, int] = (2, 7)
_PROFILE_DWELL_S_RANGE: tuple[float, float] = (3.0, 9.0)
_POST_MODAL_DWELL_S_RANGE: tuple[float, float] = (4.0, 12.0)

# ── Engagement probabilities (hardcoded per QA spec) ───────────────────
# Each post / reel rolls these INDEPENDENTLY:
#   * 30% chance to like.
#   * 45% chance to open the comments modal and engage.
# When the comments modal IS opened (either via these per-element rolls
# or because the dispatcher picked the open_comments action directly):
#   * Try to like 3-4 different unliked comments.
#   * Each attempt has a 60-70% chance of actually clicking — the
#     specific threshold is drawn fresh per session via rng.uniform
#     so two warmup runs don't have identical comment-like cadence.
_LIKE_POST_PROBABILITY: float = 0.30
_OPEN_COMMENTS_PROBABILITY: float = 0.45
_COMMENT_LIKE_CHANCE_RANGE: tuple[float, float] = (0.60, 0.70)
_COMMENT_ATTEMPTS_RANGE: tuple[int, int] = (3, 4)

# Short-timeout sweep used when a per-tick lookup fails — the regular
# defaults (1.5s × 14 selectors) are too slow for in-loop usage. With
# 0.5s × 14 the worst case is ~7s and most sweeps short-circuit on the
# first or second match.
_INLOOP_DISMISS_TIMEOUT_S: float = 0.5


# ── Helpers ────────────────────────────────────────────────────────────
# safe_coordinate_click is imported from workers.core.behavior


def _safe_find(page: Any, selector: str, *, timeout: float = 2.5) -> Any | None:
    """Return the first matching element or ``None`` — never raises."""
    try:
        return page.ele(selector, timeout=timeout)
    except Exception as exc:
        logger.debug("[warmup] selector %r raised: %s", selector, exc)
        return None


def _safe_find_all(page: Any, selector: str, *, timeout: float = 2.5) -> List[Any]:
    """Return all matching elements (possibly empty) — never raises."""
    try:
        return list(page.eles(selector, timeout=timeout) or [])
    except Exception as exc:
        logger.debug("[warmup] eles(%r) raised: %s", selector, exc)
        return []


def _is_already_liked(ele: Any) -> bool:
    """Return True iff the Like control is in the 'Unlike' state.

    Accepts EITHER the SVG icon directly OR a wrapping element
    (``<div role="button">`` / ``<button>``) that contains the SVG.
    The state-of-truth is the SVG's ``aria-label`` (it flips between
    ``Like`` ↔ ``Unlike``); the wrapping button's ``aria-label``,
    when present, often stays pinned to ``"Like"`` regardless of
    state — which would falsely report "not liked" and let us
    toggle the user's real like off.

    Resolution order:

        1. **Always look for an inner SVG first.** If found, its
           ``aria-label`` is authoritative — even if our caller
           passed a button whose own ``aria-label`` says something
           else.
        2. Only if no inner SVG exists do we read the element's own
           ``aria-label`` (the case where the caller passed an SVG
           directly).

    Fail-closed: if we can't determine state we return ``True``
    (treat as already liked) so the warmup never UN-likes a real
    post.
    """
    if ele is None:
        return True
    try:
        # 1. Authoritative: the inner SVG (if any).
        try:
            svg = ele.ele(
                'xpath:.//svg[@aria-label="Like" or @aria-label="Unlike"]',
                timeout=1,
            )
        except Exception:
            svg = None
        if svg is not None:
            inner_label = (svg.attr("aria-label") or "").strip().lower()
            if inner_label in ("like", "unlike"):
                return inner_label == "unlike"

        # 2. Fallback: caller passed the SVG itself, or any other
        # element whose own aria-label encodes state.
        label = (ele.attr("aria-label") or "").strip().lower()
        if label in ("like", "unlike"):
            return label == "unlike"

        # Unknown — fail closed.
        return True
    except Exception:
        return True



def _sweep_modals_safely(browser: InstagramBrowser, *, label: str) -> int:
    """In-loop modal sweep — short timeout, soft-fail, never propagates.

    Used inside per-tick action handlers when a lookup that *should*
    have succeeded comes back empty (typical cause: a "Turn on
    notifications" interstitial mounted between ticks and is now
    overlaying the feed). Always wrapped in try/except so a sweep
    failure can never end the warmup session.
    """
    try:
        n = dismiss_instagram_modals(
            browser.page,
            per_selector_timeout_s=_INLOOP_DISMISS_TIMEOUT_S,
            max_dismissals=2,
        )
        if n:
            logger.info("[warmup] %s: dismissed %d modal(s)", label, n)
        return n
    except Exception as exc:
        logger.debug("[warmup] %s: dismiss_instagram_modals raised (%s)", label, exc)
        return 0


def _try_like_visible_post(
    browser: InstagramBrowser,
    behavior: HumanBehaviorEngine,
    counters: Dict[str, int],
    *,
    counter_key: str,
) -> bool:
    """Find the visible Like SVG and coordinate-click it directly.

    Uses ``safe_coordinate_click`` on the SVG icon itself — the raw
    hardware click at physical coordinates bypasses pointer-events:none
    and React overlay interception that killed the old parent-walk
    pattern.

    If the SVG isn't found the first time, run a short modal sweep
    and retry once. The state check inspects the SVG's aria-label
    so we never re-click an already-liked control.

    Returns True iff a like was actually issued.
    """
    # Check if the Like SVG is present and not already in Unlike state.
    like_svg = _safe_find(browser.page, 'css:svg[aria-label="Like"]', timeout=2.0)
    if like_svg is None:
        _sweep_modals_safely(browser, label="like-button lookup miss")
        like_svg = _safe_find(browser.page, 'css:svg[aria-label="Like"]', timeout=1.5)
    if like_svg is None:
        logger.debug("[warmup] no visible Like SVG — skipping")
        return False
    if _is_already_liked(like_svg):
        logger.debug("[warmup] post already liked — skipping")
        return False

    if not safe_coordinate_click(browser.page, 'css:svg[aria-label="Like"]'):
        logger.debug("[warmup] coordinate click on Like SVG failed")
        return False

    counters[counter_key] = counters.get(counter_key, 0) + 1
    logger.info("[warmup] liked a %s (counter=%s now %d)",
                counter_key.removesuffix("_liked") or "post",
                counter_key, counters[counter_key])
    behavior.idle(0.7, 1.6)
    return True


def _engage_with_comments(
    browser: InstagramBrowser,
    behavior: HumanBehaviorEngine,
    rng: random.Random,
    counters: Dict[str, int],
    *,
    modal_counter_key: str = "comment_modals_opened",
    likes_counter_key: str = "comments_liked",
) -> bool:
    """Open the comment modal via coordinate click on the Comment SVG,
    attempt 3-4 comment likes at 60-70% per-comment chance, then close.

    Uses ``safe_coordinate_click`` for all interactions — the raw
    hardware click at physical coordinates bypasses pointer-events:none
    and React overlay interception.

    The per-comment threshold is drawn ONCE per call via
    ``rng.uniform(0.60, 0.70)`` so all comments in a single modal
    share the same chance.

    Returns:
        ``True`` iff the comment modal was successfully opened.
    """
    # Open comments via direct SVG coordinate click.
    if not safe_coordinate_click(browser.page, 'css:svg[aria-label="Comment"]'):
        _sweep_modals_safely(browser, label="comment-button lookup miss")
        if not safe_coordinate_click(browser.page, 'css:svg[aria-label="Comment"]'):
            logger.debug("[warmup] no visible Comment SVG — skipping engagement")
            return False

    counters[modal_counter_key] = counters.get(modal_counter_key, 0) + 1
    logger.info("[warmup] opened comment modal (%s now %d)",
                modal_counter_key, counters[modal_counter_key])
    behavior.idle(1.4, 2.6)

    # Read through — incremental scrolls inside the dialog.
    for _ in range(rng.randint(2, 6)):
        try:
            browser.page.scroll.down(300)
        except Exception:
            pass
        time.sleep(rng.uniform(0.9, 2.4))

    # Per QA spec: 3-4 attempts at 60-70% per-comment chance.
    target_attempts = rng.randint(*_COMMENT_ATTEMPTS_RANGE)
    per_comment_chance = rng.uniform(*_COMMENT_LIKE_CHANCE_RANGE)
    logger.info(
        "[warmup] comments engaged: target_attempts=%d, per_comment_chance=%.2f",
        target_attempts, per_comment_chance,
    )

    # Collect comment-like SVGs — the small hearts (12px / 16px).
    candidates: List[Any] = []
    for sel in COMMENT_LIKE_LOCATORS:
        candidates.extend(_safe_find_all(browser.page, sel, timeout=2.0))
    # Filter out already-liked hearts.
    candidates = [c for c in candidates if not _is_already_liked(c)]
    rng.shuffle(candidates)
    logger.debug(
        "[warmup] comment-like candidate pool: %d unliked SVG(s)",
        len(candidates),
    )

    attempts = 0
    for heart_svg in candidates:
        if attempts >= target_attempts:
            break
        attempts += 1
        if rng.random() >= per_comment_chance:
            logger.debug(
                "[warmup] comment attempt %d/%d: chance roll missed — skip",
                attempts, target_attempts,
            )
            continue
        # Coordinate click directly on the comment heart SVG.
        try:
            heart_svg.scroll.to_see(center=True)
            browser.page.wait(0.3)
            x, y = heart_svg.rect.midpoint
            browser.page.actions.move_to((x, y)).click()
            counters[likes_counter_key] = counters.get(likes_counter_key, 0) + 1
            logger.info(
                "[warmup] liked a comment (attempt %d/%d, %s now %d)",
                attempts, target_attempts, likes_counter_key,
                counters[likes_counter_key],
            )
            behavior.idle(0.7, 1.7)
        except Exception as exc:
            logger.debug("[warmup] comment-like coordinate click failed: %s", exc)

    # Close the modal — best effort via coordinate click on Close SVG.
    safe_coordinate_click(browser.page, 'css:svg[aria-label="Close"]', timeout=2)
    behavior.idle(0.6, 1.3)
    return True


# ── Action: scroll the feed ────────────────────────────────────────────
def _action_scroll_feed(
    browser: InstagramBrowser,
    behavior: HumanBehaviorEngine,
    rng: random.Random,
    counters: Dict[str, int],
) -> None:
    # Pre-scroll modal sweep — IG sometimes mounts the "Turn on
    # notifications" interstitial between ticks.
    posts_visible = _safe_find(browser.page, "css:article", timeout=1.0)
    if posts_visible is None:
        _sweep_modals_safely(browser, label="scroll_feed pre-tick")

    # INCREMENTAL SCROLLING: scroll down by 500px, wait 1-2s, scan for
    # elements in the current viewport. No bulk smooth_scroll — that
    # teleports the viewport and misses lazy-loaded content.
    for _ in range(rng.randint(2, 5)):
        try:
            browser.page.scroll.down(500)
        except Exception as exc:
            logger.debug("[warmup] scroll.down(500) failed: %s", exc)
        time.sleep(rng.uniform(1.0, 2.0))

    # Per-spec: 30% chance to like the visible post.
    if rng.random() < _LIKE_POST_PROBABILITY:
        _try_like_visible_post(
            browser, behavior, counters, counter_key="posts_liked",
        )

    # Per-spec: 45% chance to open the comments modal and engage
    # (3-4 attempts at 60-70% per-comment chance, handled inside the
    # helper). Independent of the like roll above — both can fire.
    if rng.random() < _OPEN_COMMENTS_PROBABILITY:
        _engage_with_comments(browser, behavior, rng, counters)


# ── Action: open comment thread on the visible post ────────────────────
def _action_open_comments(
    browser: InstagramBrowser,
    behavior: HumanBehaviorEngine,
    rng: random.Random,
    counters: Dict[str, int],
) -> None:
    """Top-level dispatcher action — always engages with comments
    (no per-tick gate; the dispatcher already rolled to pick this).
    """
    _engage_with_comments(browser, behavior, rng, counters)


# ── Action: hop into Reels and watch a few ─────────────────────────────
def _action_watch_reels(
    browser: InstagramBrowser,
    behavior: HumanBehaviorEngine,
    rng: random.Random,
    counters: Dict[str, int],
) -> None:
    # Use navigate_left_rail so the Reels tab gets the React-mandated
    # hover hydration before the click — direct clicks on the
    # un-hydrated icon silently no-op or hit the wrong target.
    if not behavior.navigate_left_rail("reels"):
        logger.debug("[warmup] navigate_left_rail('reels') failed; skipping tick")
        return
    behavior.idle(2.0, 4.0)
    counters["reels_sessions"] += 1

    n_reels = rng.randint(*_REELS_PER_VISIT_RANGE)
    for _ in range(n_reels):
        # Watch — sleep is the point, no scrolling.
        watch_s = rng.uniform(*_REEL_WATCH_S_RANGE)
        time.sleep(watch_s)
        counters["reels_watched"] += 1
        counters["reels_watch_seconds"] = int(
            counters.get("reels_watch_seconds", 0) + watch_s
        )

        # Per-spec: like and open-comments are now INDEPENDENT rolls,
        # not exclusive branches of a single uniform draw.
        #   * 30% chance to like the reel.
        #   * 45% chance to open the comments modal and engage
        #     (3-4 attempts at 60-70% per-comment chance).
        if rng.random() < _LIKE_POST_PROBABILITY:
            _try_like_visible_post(
                browser, behavior, counters, counter_key="reels_liked",
            )

        if rng.random() < _OPEN_COMMENTS_PROBABILITY:
            _engage_with_comments(
                browser, behavior, rng, counters,
                modal_counter_key="reels_comment_modals",
                likes_counter_key="comments_liked",
            )

        # Next reel — coordinate click on the navigation arrow.
        if not safe_coordinate_click(browser.page, NEXT_REEL_LOCATOR, timeout=2):
            # If we can't advance via the button, an incremental scroll
            # is the keyboard-less native gesture.
            try:
                browser.page.scroll.down(800)
            except Exception:
                pass
        behavior.idle(0.4, 1.2)


# ── Action: visit a random profile ─────────────────────────────────────
def _action_visit_profile(
    browser: InstagramBrowser,
    behavior: HumanBehaviorEngine,
    rng: random.Random,
    counters: Dict[str, int],
) -> None:
    candidates = _safe_find_all(browser.page, PROFILE_LINK_LOCATOR, timeout=3.0)
    if not candidates:
        return
    target = rng.choice(candidates)
    if not behavior.safe_click(target):
        return
    counters["profiles_visited"] += 1
    behavior.idle(*_PROFILE_DWELL_S_RANGE)

    # Skim the grid briefly — incremental scrolling.
    for _ in range(rng.randint(1, 3)):
        try:
            browser.page.scroll.down(500)
        except Exception:
            pass
        time.sleep(rng.uniform(1.0, 2.0))

    # Maybe open the first post / reel and look at it.
    if rng.random() < 0.55:
        first = _safe_find(browser.page, PROFILE_FIRST_POST_LOCATOR, timeout=2.0)
        if first is not None and behavior.safe_click(first):
            counters["profile_posts_opened"] += 1
            behavior.idle(*_POST_MODAL_DWELL_S_RANGE)

            # Scroll inside the modal a bit.
            for _ in range(rng.randint(1, 3)):
                try:
                    browser.page.scroll.down(300)
                except Exception:
                    pass
                time.sleep(rng.uniform(0.8, 1.5))

            safe_coordinate_click(browser.page, 'css:svg[aria-label="Close"]', timeout=2)
            behavior.idle(0.7, 1.6)

    # Browser-Back to the feed.
    try:
        browser.page.back()
    except Exception as exc:
        logger.debug("[warmup] page.back() failed (%s); navigating home", exc)
        try:
            browser.page.get(DEFAULT_FEED_URL)
        except Exception as exc2:
            logger.debug("[warmup] feed-recovery nav also failed (%s)", exc2)
    behavior.idle(1.4, 2.8)


# ── Dispatch table ─────────────────────────────────────────────────────
_ActionFn = Callable[
    [InstagramBrowser, HumanBehaviorEngine, random.Random, Dict[str, int]], None
]
_ACTIONS: Dict[str, _ActionFn] = {
    "scroll_feed":   _action_scroll_feed,
    "open_comments": _action_open_comments,
    "watch_reels":   _action_watch_reels,
    "visit_profile": _action_visit_profile,
}


def _pick_action(weights: Dict[str, float], rng: random.Random) -> str:
    keys = list(weights.keys())
    vals = [max(0.0, float(weights[k])) for k in keys]
    if not any(vals):
        return "scroll_feed"  # safest fallback if user zeroed everything
    return rng.choices(keys, weights=vals, k=1)[0]


# ── Public entrypoint ──────────────────────────────────────────────────
def execute_warmup(
    browser: InstagramBrowser,
    args: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Drive a time-bounded, weighted-random IG warmup session.

    The browser is owned by the caller (``TaskExecutor``); this function
    never instantiates a new one and never calls ``browser.close()``.
    Per-tick failures are swallowed so a transient missing locator
    doesn't end a 15-minute session — the next dice roll just picks
    another action. Configuration errors and outright dead browsers
    propagate.
    """
    args = args or {}
    rng = random.Random()

    feed_url = str(args.get("feed_url", DEFAULT_FEED_URL))
    page_load_wait_s = float(args.get("page_load_wait_s", DEFAULT_PAGE_LOAD_WAIT_S))
    duration_minutes = float(args.get("duration_minutes", DEFAULT_DURATION_MINUTES))
    if duration_minutes <= 0:
        raise ValueError(f"duration_minutes must be > 0, got {duration_minutes}")

    # Merge user overrides on top of defaults; unknown keys are ignored.
    weights = dict(DEFAULT_WEIGHTS)
    for k, v in (args.get("action_weights") or {}).items():
        if k in weights:
            weights[k] = float(v)

    end_time = time.monotonic() + duration_minutes * 60.0

    logger.info(
        "[warmup] navigating to %s (duration=%.1fmin, weights=%s)",
        feed_url, duration_minutes, weights,
    )
    browser.page.get(feed_url)
    time.sleep(page_load_wait_s)

    # ── AGGRESSIVE MODAL DISMISSAL ─────────────────────────────────────
    # The very first thing IG often shows after the feed renders is the
    # "Turn on notifications" interstitial. If we don't clear it BEFORE
    # the warmup loop starts, every per-tick lookup (like buttons,
    # comment icons, the Reels rail) fires against a blocked viewport
    # and silently no-ops. This is a FULL sweep with the standalone
    # helper's default 1.5s/selector budget — we want to be thorough
    # here, not fast.
    initial_dismissals = 0
    try:
        initial_dismissals = dismiss_instagram_modals(browser.page)
    except Exception as exc:
        logger.warning(
            "[warmup] initial dismiss_instagram_modals raised (%s); continuing",
            exc,
        )
    if initial_dismissals:
        logger.info(
            "[warmup] cleared %d pre-loop modal(s) (e.g. 'Turn on notifications')",
            initial_dismissals,
        )

    behavior = HumanBehaviorEngine(browser.page)
    # Initial "I just opened the app" beat — humans don't engage instantly.
    behavior.idle(2.0, 4.0)

    counters: Dict[str, int] = {
        "ticks":                  0,
        "posts_liked":            0,
        "comment_modals_opened":  0,
        "comments_liked":         0,
        "reels_sessions":         0,
        "reels_watched":          0,
        "reels_watch_seconds":    0,
        "reels_liked":            0,
        "reels_comment_modals":   0,
        "profiles_visited":       0,
        "profile_posts_opened":   0,
    }
    action_log: List[Dict[str, Any]] = []

    while time.monotonic() < end_time:
        # ── ROBOTS.TXT GUARD ───────────────────────────────────────────
        # If a prior tick crashed and somehow re-navigated to the
        # cookie-injection domain (/robots.txt), force recovery to the
        # feed. Without this the entire remaining session runs against
        # the wrong page and every locator silently fails.
        try:
            current_url = browser.page.url or ""
            if "robots.txt" in current_url or not current_url.startswith("https://www.instagram.com"):
                logger.warning(
                    "[warmup] URL guard triggered (url=%r) — navigating back to feed",
                    current_url,
                )
                browser.page.get(feed_url)
                time.sleep(page_load_wait_s)
                # Re-sweep modals after forced navigation.
                try:
                    dismiss_instagram_modals(browser.page)
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("[warmup] URL guard check failed (%s); continuing", exc)

        action_name = _pick_action(weights, rng)
        action_fn = _ACTIONS[action_name]
        tick_started_at = time.monotonic()
        logger.info("[warmup] tick %d: %s", counters["ticks"] + 1, action_name)

        try:
            action_fn(browser, behavior, rng, counters)
            ok = True
            err: Optional[str] = None
        except Exception as exc:
            # Per-tick guard — a single missing locator must NOT end the
            # session. We log it, count it, and roll again.
            ok = False
            err = f"{type(exc).__name__}: {exc}"
            logger.warning("[warmup] tick %r failed (%s) — continuing", action_name, err)

        counters["ticks"] += 1
        action_log.append({
            "action":     action_name,
            "ok":         ok,
            "elapsed_s":  round(time.monotonic() - tick_started_at, 2),
            "error":      err,
        })

        # Inter-tick breath. Long enough to break up timing fingerprints,
        # short enough that we still hit the duration budget.
        behavior.idle(1.2, 3.4)

    logger.info(
        "[warmup] session done after %d ticks: %s",
        counters["ticks"], {k: v for k, v in counters.items() if v},
    )
    return {
        "action":            "warmup",
        "version":           2,
        "feed_url":          feed_url,
        "duration_minutes":  duration_minutes,
        "weights":           weights,
        "counters":          counters,
        "action_log":        action_log,
    }


# ── Standalone smoke-test (not used in production) ─────────────────────
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
    print("  Instagram Worker - Warmup 2.0 Smoke Test")
    print("=" * 55)

    browser: InstagramBrowser | None = None
    try:
        browser = InstagramBrowser(
            proxy_string=_SMOKE_TEST_PROXY,
            user_agent=_SMOKE_TEST_USER_AGENT,
            headless=False,
        )
        browser.inject_cookies(_SMOKE_TEST_COOKIES)
        result = execute_warmup(browser, args={"duration_minutes": 1.5})
        print(f"[+] Warmup result: {result}")
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
