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

from workers.core.behavior import HumanBehaviorEngine
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

# Per-tick budgets — the loop stops cleanly between ticks so a long Reel
# watch can't blow the overall duration_minutes budget by more than ~30s.
_REEL_WATCH_S_RANGE: tuple[float, float] = (5.0, 30.0)
_REELS_PER_VISIT_RANGE: tuple[int, int] = (2, 7)
_COMMENT_LIKES_RANGE: tuple[int, int] = (1, 3)
_PROFILE_DWELL_S_RANGE: tuple[float, float] = (3.0, 9.0)
_POST_MODAL_DWELL_S_RANGE: tuple[float, float] = (4.0, 12.0)


# ── Helpers ────────────────────────────────────────────────────────────
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


def _is_already_liked(svg_ele: Any) -> bool:
    """Return True iff the heart icon is in the 'Unlike' state.

    The post-like SVG flips its ``aria-label`` between ``Like`` and
    ``Unlike`` — we only ever click on a ``Like`` heart. This guard is
    paranoid because mis-toggling the user's real activity is the one
    thing a warmup must NEVER do.
    """
    try:
        label = (svg_ele.attr("aria-label") or "").strip().lower()
    except Exception:
        return True  # fail closed — don't click if we can't tell
    return label == "unlike"


# ── Action: scroll the feed ────────────────────────────────────────────
def _action_scroll_feed(
    browser: InstagramBrowser,
    behavior: HumanBehaviorEngine,
    rng: random.Random,
    counters: Dict[str, int],
) -> None:
    # 2-5 smooth scrolls in a row, with one optional "like the post under
    # me right now" sprinkled in.
    for _ in range(rng.randint(2, 5)):
        behavior.smooth_scroll(min_y=300, max_y=900, upward_probability=0.18)

    if rng.random() < 0.35:
        like_btn = _safe_find(browser.page, POST_LIKE_ICON_LOCATOR, timeout=2.0)
        if like_btn is not None and not _is_already_liked(like_btn):
            if behavior.safe_click(like_btn):
                counters["posts_liked"] += 1
                behavior.idle(0.8, 1.8)


# ── Action: open comment thread on the visible post ────────────────────
def _action_open_comments(
    browser: InstagramBrowser,
    behavior: HumanBehaviorEngine,
    rng: random.Random,
    counters: Dict[str, int],
) -> None:
    icon = _safe_find(browser.page, COMMENT_ICON_LOCATOR, timeout=3.0)
    if icon is None or not behavior.safe_click(icon):
        return
    counters["comment_modals_opened"] += 1
    behavior.idle(1.4, 2.6)

    # Read through — smooth scrolls inside the dialog.
    for _ in range(rng.randint(2, 6)):
        behavior.smooth_scroll(
            min_y=180, max_y=420,
            upward_probability=0.10,
            post_scroll_pause_range_s=(0.9, 2.4),
        )

    # Like 1-3 random comments, but never re-toggle an already-liked one.
    target_likes = rng.randint(*_COMMENT_LIKES_RANGE)
    candidates: List[Any] = []
    for sel in COMMENT_LIKE_LOCATORS:
        candidates.extend(_safe_find_all(browser.page, sel, timeout=2.0))
    rng.shuffle(candidates)

    for heart in candidates:
        if target_likes <= 0:
            break
        if _is_already_liked(heart):
            continue
        if behavior.safe_click(heart):
            counters["comments_liked"] += 1
            target_likes -= 1
            behavior.idle(0.7, 1.7)

    # Close the modal.
    close_btn = _safe_find(browser.page, CLOSE_ICON_LOCATOR, timeout=2.0)
    behavior.safe_click(close_btn)
    behavior.idle(0.6, 1.3)


# ── Action: hop into Reels and watch a few ─────────────────────────────
def _action_watch_reels(
    browser: InstagramBrowser,
    behavior: HumanBehaviorEngine,
    rng: random.Random,
    counters: Dict[str, int],
) -> None:
    reels_tab = _safe_find(browser.page, REELS_TAB_LOCATOR, timeout=3.0)
    if reels_tab is None or not behavior.safe_click(reels_tab):
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

        # Maybe like or open comments while watching.
        roll = rng.random()
        if roll < 0.25:
            heart = _safe_find(browser.page, POST_LIKE_ICON_LOCATOR, timeout=1.5)
            if heart is not None and not _is_already_liked(heart):
                if behavior.safe_click(heart):
                    counters["reels_liked"] += 1
                    behavior.idle(0.6, 1.4)
        elif roll < 0.40:
            icon = _safe_find(browser.page, COMMENT_ICON_LOCATOR, timeout=1.5)
            if icon is not None and behavior.safe_click(icon):
                counters["reels_comment_modals"] += 1
                behavior.idle(2.0, 5.0)
                close_btn = _safe_find(browser.page, CLOSE_ICON_LOCATOR, timeout=2.0)
                behavior.safe_click(close_btn)

        # Next reel.
        next_btn = _safe_find(browser.page, NEXT_REEL_LOCATOR, timeout=2.0)
        if next_btn is None or not behavior.safe_click(next_btn):
            # If we can't advance via the button, a smooth downward scroll
            # is the keyboard-less native gesture.
            behavior.smooth_scroll(min_y=600, max_y=1100, upward_probability=0.0)
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

    # Skim the grid briefly.
    for _ in range(rng.randint(1, 3)):
        behavior.smooth_scroll(min_y=300, max_y=800, upward_probability=0.10)

    # Maybe open the first post / reel and look at it.
    if rng.random() < 0.55:
        first = _safe_find(browser.page, PROFILE_FIRST_POST_LOCATOR, timeout=2.0)
        if first is not None and behavior.safe_click(first):
            counters["profile_posts_opened"] += 1
            behavior.idle(*_POST_MODAL_DWELL_S_RANGE)

            # Scroll inside the modal a bit.
            for _ in range(rng.randint(1, 3)):
                behavior.smooth_scroll(min_y=120, max_y=360, upward_probability=0.10)

            close_btn = _safe_find(browser.page, CLOSE_ICON_LOCATOR, timeout=2.0)
            behavior.safe_click(close_btn)
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
