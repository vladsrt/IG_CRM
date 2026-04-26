"""
Warmup Action Script
--------------------
Performs a low-risk "I am a real human" feed-scroll session against an
already-authenticated Instagram tab.

Public API
~~~~~~~~~~
``execute_warmup(browser, args)`` — invoked by ``TaskExecutor``. The browser
is owned by the executor; this module never instantiates one in production.

The ``__main__`` block at the bottom is a standalone smoke-test that wires up
its own browser/cookies — handy when iterating on this script in isolation.
"""

from __future__ import annotations

import logging
import os
import random
import sys
import time
from typing import Any, Dict

# Add backend directory to module search path so `workers` is resolvable
# when this file is executed directly via `python action_warmup.py`.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from workers.core.browser_core import InstagramBrowser

logger = logging.getLogger(__name__)


# ── Defaults ────────────────────────────────────────────────────────────
DEFAULT_FEED_URL: str = "https://www.instagram.com/"
DEFAULT_PAGE_LOAD_WAIT_S: float = 5.0
DEFAULT_SCROLL_STEPS_MIN: int = 2
DEFAULT_SCROLL_STEPS_MAX: int = 4
DEFAULT_SCROLL_PX_MIN: int = 300
DEFAULT_SCROLL_PX_MAX: int = 800
DEFAULT_PAUSE_S_MIN: float = 2.5
DEFAULT_PAUSE_S_MAX: float = 6.0


# ── Public action handler ───────────────────────────────────────────────
def execute_warmup(
    browser: InstagramBrowser,
    args: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    Perform a randomized feed-scroll warmup using the **caller-owned** browser.

    The executor is responsible for browser lifecycle (creation, cookie
    injection, teardown). This function never instantiates or closes a
    browser, and never swallows exceptions — it lets them propagate so the
    executor can mark the parent Task as FAILED.

    Args:
        browser: An already-launched ``InstagramBrowser`` with valid cookies.
        args: Optional tuning knobs:
            * ``feed_url`` (str)         — URL to scroll. Defaults to IG home.
            * ``scroll_steps_min`` (int) — Lower bound on scroll repetitions.
            * ``scroll_steps_max`` (int) — Upper bound on scroll repetitions.
            * ``scroll_px_min`` (int)    — Lower bound on scroll distance per step.
            * ``scroll_px_max`` (int)    — Upper bound on scroll distance per step.
            * ``pause_s_min`` (float)    — Lower bound on per-step pause.
            * ``pause_s_max`` (float)    — Upper bound on per-step pause.
            * ``page_load_wait_s`` (float) — Initial wait after navigation.

    Returns:
        Diagnostic dict consumed by ``TaskExecutor`` and persisted in the
        Celery result backend.
    """
    args = args or {}

    feed_url = str(args.get("feed_url", DEFAULT_FEED_URL))
    page_load_wait_s = float(args.get("page_load_wait_s", DEFAULT_PAGE_LOAD_WAIT_S))

    steps_min = int(args.get("scroll_steps_min", DEFAULT_SCROLL_STEPS_MIN))
    steps_max = int(args.get("scroll_steps_max", DEFAULT_SCROLL_STEPS_MAX))
    if steps_min < 1 or steps_max < steps_min:
        raise ValueError(
            f"Invalid scroll_steps bounds: min={steps_min}, max={steps_max}"
        )

    px_min = int(args.get("scroll_px_min", DEFAULT_SCROLL_PX_MIN))
    px_max = int(args.get("scroll_px_max", DEFAULT_SCROLL_PX_MAX))
    if px_min < 1 or px_max < px_min:
        raise ValueError(f"Invalid scroll_px bounds: min={px_min}, max={px_max}")

    pause_min = float(args.get("pause_s_min", DEFAULT_PAUSE_S_MIN))
    pause_max = float(args.get("pause_s_max", DEFAULT_PAUSE_S_MAX))
    if pause_min < 0 or pause_max < pause_min:
        raise ValueError(
            f"Invalid pause bounds: min={pause_min}, max={pause_max}"
        )

    logger.info("[warmup] navigating to %s", feed_url)
    browser.page.get(feed_url)
    time.sleep(page_load_wait_s)

    scroll_steps = random.randint(steps_min, steps_max)
    logger.info("[warmup] scrolling %d step(s)", scroll_steps)

    scroll_log = []
    for i in range(scroll_steps):
        scroll_amount = random.randint(px_min, px_max)
        logger.debug(
            "[warmup] scroll step %d/%d: down %dpx", i + 1, scroll_steps, scroll_amount
        )
        browser.page.scroll.down(scroll_amount)

        pause = random.uniform(pause_min, pause_max)
        time.sleep(pause)
        scroll_log.append({"step": i + 1, "px": scroll_amount, "pause_s": round(pause, 2)})

    logger.info("[warmup] completed %d scroll step(s)", scroll_steps)
    return {
        "action": "warmup",
        "feed_url": feed_url,
        "scroll_steps": scroll_steps,
        "scroll_log": scroll_log,
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
    """Original ``run_warmup`` behavior — kept for manual debugging only."""
    print("=" * 55)
    print("  Instagram Worker - Standalone Warmup Smoke Test")
    print("=" * 55)

    browser: InstagramBrowser | None = None
    try:
        browser = InstagramBrowser(
            proxy_string=_SMOKE_TEST_PROXY,
            user_agent=_SMOKE_TEST_USER_AGENT,
            headless=False,
        )
        browser.inject_cookies(_SMOKE_TEST_COOKIES)
        result = execute_warmup(browser, args={})
        print(f"[+] Warmup result: {result}")
    except Exception as exc:  # smoke-test only — production uses TaskExecutor
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
