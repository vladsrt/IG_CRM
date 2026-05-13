"""Gather stats action.

Lightweight, headless-only action for the dedicated stats worker. Opens the
account's profile page, navigates to the Reels tab, and scrolls a few times
to trigger Instagram's GraphQL pagination.

No DOM parsing is needed here. The ObservabilityMonitor (running as a daemon
thread inside TaskExecutor) intercepts all GraphQL and private-API JSON
responses from the network layer. When Instagram returns payloads containing
follower_count, reach, or play_count, the monitor buffers them automatically.
After the browser closes, TaskExecutor._persist_and_analyze() flushes the
buffered samples into the account_metrics table.

This network-level approach is resilient to DOM layout changes. As long as
Instagram's API response shape stays the same, stats collection keeps working
regardless of frontend redesigns.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

from workers.core.behavior import HumanBehaviorEngine, dismiss_instagram_modals
from workers.core.browser_core import InstagramBrowser

logger = logging.getLogger(__name__)

# defaults
PROFILE_URL_TEMPLATE: str = "https://www.instagram.com/{username}/"
DEFAULT_PAGE_LOAD_WAIT_S: float = 4.0
DEFAULT_SCROLL_PASSES: int = 3
REELS_TAB_SELECTORS: list[str] = [
    'xpath://a[contains(@href,"/reels/")]',
    'css:svg[aria-label="Reels"]',
    'xpath://span[normalize-space()="Reels"]',
]


def _safe_find(page: Any, selector: str, *, timeout: float = 3.0) -> Any | None:
    """Find an element without raising if it's missing."""
    try:
        ele = page.ele(selector, timeout=timeout)
        return ele if ele else None
    except Exception as exc:
        logger.debug("[gather_stats] selector %r raised: %s", selector, exc)
        return None


def execute_gather_stats(
    browser: InstagramBrowser,
    args: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Navigate to profile, open Reels, scroll to trigger GraphQL responses.

    The ObservabilityMonitor network listener (attached by TaskExecutor)
    intercepts the GraphQL JSON responses in the background. This handler
    does NOT parse the DOM for metrics. Its only job is to make the browser
    visit the right pages so Instagram's servers return the data we want.

    Args:
        browser: live InstagramBrowser session with cookies already injected.
        args: optional overrides. Supported keys:
            - ig_username (str): the account's IG handle.
            - scroll_passes (int): number of micro_scroll rounds (default 3).
            - page_load_wait_s (float): seconds to wait after navigation.

    Returns:
        dict with action name and scroll count.
    """
    args = args or {}
    ig_username = args.get("ig_username", "")
    scroll_passes = int(args.get("scroll_passes", DEFAULT_SCROLL_PASSES))
    page_load_wait_s = float(args.get("page_load_wait_s", DEFAULT_PAGE_LOAD_WAIT_S))

    behavior = HumanBehaviorEngine(browser.page)

    # step 1: navigate to the account's profile page
    if ig_username:
        profile_url = PROFILE_URL_TEMPLATE.format(username=ig_username)
        logger.info("[gather_stats] navigating to profile: %s", profile_url)
        browser.page.get(profile_url)
    else:
        # fallback: use the left-rail profile nav
        logger.info("[gather_stats] no username given, using left-rail navigation")
        behavior.navigate_left_rail("profile")

    time.sleep(page_load_wait_s)

    # step 2: dismiss any modals that appeared on page load
    try:
        dismissed = dismiss_instagram_modals(browser.page, max_dismissals=2)
        if dismissed:
            logger.info("[gather_stats] dismissed %d modal(s)", dismissed)
    except Exception as exc:
        logger.debug("[gather_stats] modal sweep raised (%s), continuing", exc)

    behavior.idle(1.0, 2.0)

    # step 3: click the Reels tab to load reel-level metrics
    reels_clicked = False
    for selector in REELS_TAB_SELECTORS:
        tab = _safe_find(browser.page, selector, timeout=3.0)
        if tab is not None:
            try:
                tab.click(by_js=True)
                reels_clicked = True
                logger.info("[gather_stats] Reels tab clicked via %r", selector)
                break
            except Exception as exc:
                logger.debug(
                    "[gather_stats] Reels tab click via %r failed (%s)", selector, exc
                )

    if not reels_clicked:
        # fallback: try left-rail Reels navigation
        logger.info("[gather_stats] tab click failed, trying left-rail Reels")
        reels_clicked = behavior.navigate_left_rail("reels")

    if not reels_clicked:
        logger.warning("[gather_stats] could not reach Reels tab — scrolling profile anyway")

    behavior.idle(2.0, 3.5)

    # step 4: scroll to trigger GraphQL pagination.
    # each scroll fires lazy-load requests that carry view_count, play_count,
    # and follower_count in the JSON responses. the ObservabilityMonitor
    # intercepts these automatically — we just need to trigger the requests.
    actual_scrolls = 0
    for i in range(scroll_passes):
        behavior.micro_scroll(
            count_range=(2, 4),
            pixel_range=(200, 500),
            upward_probability=0.1,
        )
        actual_scrolls += 1
        behavior.idle(1.5, 3.0)
        logger.info("[gather_stats] scroll pass %d/%d done", i + 1, scroll_passes)

    # step 5: one final pause so in-flight network requests complete
    # before TaskExecutor stops the ObservabilityMonitor
    behavior.idle(2.0, 4.0)

    logger.info(
        "[gather_stats] finished: username=%s, reels_clicked=%s, scrolls=%d",
        ig_username or "(via nav)",
        reels_clicked,
        actual_scrolls,
    )

    return {
        "action": "gather_stats",
        "ig_username": ig_username or None,
        "reels_tab_reached": reels_clicked,
        "scroll_passes": actual_scrolls,
    }
