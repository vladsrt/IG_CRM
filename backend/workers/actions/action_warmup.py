"""
Warmup Action — v2 ("Warmup 2.0")
---------------------------------
Drives a humanized feed-browsing session against an already-authenticated
``InstagramBrowser``. The goal is *not* to do anything observable on the
account; it is to make the account look alive to Meta's risk engine —
spending realistic time on the feed, reading posts, looking at comments,
liking the occasional thing, with no two sessions ever looking identical.

Public API
~~~~~~~~~~
``execute_warmup(browser, args)`` — invoked by ``TaskExecutor``. The
browser is owned by the executor; this module never instantiates one in
production. The action never swallows exceptions; it lets them propagate
so the executor can mark the parent Task FAILED.

Behaviour mix
~~~~~~~~~~~~~
Every session is composed of multiple "phases" (3-6 of them) drawn from
a weighted pool. Each phase has its own randomized duration. The phase
order itself is shuffled, so a session might start with a fast skim and
end with a long read, or the other way around. Within each phase,
:meth:`HumanBehaviorEngine.deep_scroll_session` mixes slow reads, skims,
flicks, and upward corrections with their own weighted dice rolls.

Why so much randomness? Because uniform behaviour is itself a tell.
Two warmup sessions that scroll-down-N-pixels-pause-M-seconds at the
exact same cadence are indistinguishable from a script even when each
delta is humanized in isolation. The variance has to compound.

Tunable args (all optional)
~~~~~~~~~~~~~~~~~~~~~~~~~~~
* ``feed_url`` (str)            — URL to start on. Default IG home.
* ``page_load_wait_s`` (float)  — Initial wait after navigation.
* ``total_minutes_min`` (float) — Lower bound on overall session time.
* ``total_minutes_max`` (float) — Upper bound on overall session time.
* ``phases_min`` (int)          — Lower bound on number of phases.
* ``phases_max`` (int)          — Upper bound on number of phases.
* ``like_probability`` (float)  — Per-tick probability of liking a post.
* ``open_comments_probability`` (float) — Per-tick probability of
                                          opening a comments modal.
"""

from __future__ import annotations

import logging
import os
import random
import sys
import time
from typing import Any, Dict, List

# Add backend directory to module search path so `workers` is resolvable
# when this file is executed directly via `python action_warmup.py`.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from workers.core.behavior import HumanBehaviorEngine
from workers.core.browser_core import InstagramBrowser

logger = logging.getLogger(__name__)


# ── Defaults ────────────────────────────────────────────────────────────
DEFAULT_FEED_URL: str = "https://www.instagram.com/"
DEFAULT_PAGE_LOAD_WAIT_S: float = 5.0
DEFAULT_TOTAL_MINUTES_MIN: float = 3.0
DEFAULT_TOTAL_MINUTES_MAX: float = 8.0
DEFAULT_PHASES_MIN: int = 3
DEFAULT_PHASES_MAX: int = 6
DEFAULT_LIKE_PROBABILITY: float = 0.22
DEFAULT_OPEN_COMMENTS_PROBABILITY: float = 0.30

# Phase profiles — each is a multiplier set the deep_scroll_session uses
# to bias its own dice rolls. We pick from this pool with weights, and
# every session is built from a *shuffled* sequence of phases so the
# rhythm of the session itself varies.
_PHASE_POOL: List[Dict[str, Any]] = [
    {"name": "casual_skim",     "upward": 0.10, "flick": 0.08, "weight": 3},
    {"name": "deep_read",       "upward": 0.22, "flick": 0.04, "weight": 3},
    {"name": "fast_flick",      "upward": 0.06, "flick": 0.30, "weight": 2},
    {"name": "back_and_forth",  "upward": 0.34, "flick": 0.10, "weight": 2},
    {"name": "patient_browse",  "upward": 0.18, "flick": 0.06, "weight": 3},
]


def _pick_phase(rng: random.Random) -> Dict[str, Any]:
    bag: List[Dict[str, Any]] = []
    for profile in _PHASE_POOL:
        bag.extend([profile] * int(profile["weight"]))
    return rng.choice(bag)


# ── Public action handler ───────────────────────────────────────────────
def execute_warmup(
    browser: InstagramBrowser,
    args: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Drive a randomized, multi-phase feed-browsing session.

    The browser is owned by the caller (``TaskExecutor``); this function
    never instantiates a new one and never calls ``browser.close()``.
    Step failures propagate.
    """
    args = args or {}
    rng = random.Random()

    feed_url = str(args.get("feed_url", DEFAULT_FEED_URL))
    page_load_wait_s = float(args.get("page_load_wait_s", DEFAULT_PAGE_LOAD_WAIT_S))

    total_min = float(args.get("total_minutes_min", DEFAULT_TOTAL_MINUTES_MIN))
    total_max = float(args.get("total_minutes_max", DEFAULT_TOTAL_MINUTES_MAX))
    if total_min <= 0 or total_max < total_min:
        raise ValueError(
            f"Invalid total_minutes bounds: min={total_min}, max={total_max}"
        )

    phases_min = int(args.get("phases_min", DEFAULT_PHASES_MIN))
    phases_max = int(args.get("phases_max", DEFAULT_PHASES_MAX))
    if phases_min < 1 or phases_max < phases_min:
        raise ValueError(
            f"Invalid phases bounds: min={phases_min}, max={phases_max}"
        )

    like_probability = float(args.get("like_probability", DEFAULT_LIKE_PROBABILITY))
    open_comments_probability = float(
        args.get("open_comments_probability", DEFAULT_OPEN_COMMENTS_PROBABILITY)
    )

    total_seconds = rng.uniform(total_min * 60.0, total_max * 60.0)
    n_phases = rng.randint(phases_min, phases_max)

    # Distribute total_seconds across phases unevenly. Dirichlet-ish
    # mix: random weights, normalize, multiply. This ensures one phase
    # might take 60% of the session and another 5%, like real attention.
    raw_weights = [rng.uniform(0.5, 2.0) for _ in range(n_phases)]
    weight_sum = sum(raw_weights)
    phase_durations = [w / weight_sum * total_seconds for w in raw_weights]

    phases: List[Dict[str, Any]] = []
    for dur in phase_durations:
        profile = _pick_phase(rng)
        phases.append({**profile, "duration_s": dur})
    rng.shuffle(phases)  # phase order itself varies — no two sessions look alike

    logger.info(
        "[warmup] starting session total=%.1fs phases=%d profile_seq=%s",
        total_seconds,
        n_phases,
        [p["name"] for p in phases],
    )

    # ── Navigate ───────────────────────────────────────────────────────
    browser.page.get(feed_url)
    time.sleep(page_load_wait_s)

    behavior = HumanBehaviorEngine(browser.page)
    # Initial "I just opened the app" beat — humans don't engage instantly.
    behavior.read_pause(content_length=None)
    behavior.micro_scroll()

    aggregate: Dict[str, int] = {
        "slow_reads": 0,
        "skims": 0,
        "flicks": 0,
        "upward_corrections": 0,
        "posts_liked": 0,
        "comment_modals_opened": 0,
        "comments_liked": 0,
    }
    phase_log: List[Dict[str, Any]] = []

    # ── Phase loop ─────────────────────────────────────────────────────
    for idx, phase in enumerate(phases, 1):
        logger.info(
            "[warmup] phase %d/%d: %s (~%.1fs)",
            idx, len(phases), phase["name"], phase["duration_s"],
        )

        scroll_counts = behavior.deep_scroll_session(
            duration_s=phase["duration_s"],
            upward_correction_probability=phase["upward"],
            flick_probability=phase["flick"],
        )
        for k, v in scroll_counts.items():
            aggregate[k] = aggregate.get(k, 0) + v

        # Inter-phase engagement burst — 1-3 chances to like / open comments.
        burst_count = rng.randint(1, 3)
        for _ in range(burst_count):
            try:
                if behavior.maybe_like_visible_post(like_probability=like_probability):
                    aggregate["posts_liked"] += 1
            except Exception as exc:
                logger.debug("[warmup] like attempt raised (%s); ignoring", exc)

            try:
                comment_stats = behavior.browse_comments(
                    open_probability=open_comments_probability,
                )
                aggregate["comment_modals_opened"] += comment_stats.get("opened", 0)
                aggregate["comments_liked"] += comment_stats.get("comments_liked", 0)
            except Exception as exc:
                logger.debug("[warmup] browse_comments raised (%s); ignoring", exc)

            behavior.micro_scroll()
            behavior.idle(0.6, 1.8)

        phase_log.append(
            {
                "name": phase["name"],
                "duration_s": round(phase["duration_s"], 2),
                "scroll_counts": scroll_counts,
            }
        )

    logger.info(
        "[warmup] session done: %s",
        {k: v for k, v in aggregate.items() if v},
    )
    return {
        "action": "warmup",
        "version": 2,
        "feed_url": feed_url,
        "total_seconds": round(total_seconds, 1),
        "phase_count": len(phases),
        "phase_log": phase_log,
        "aggregate": aggregate,
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
    print("  Instagram Worker - Standalone Warmup 2.0 Smoke Test")
    print("=" * 55)

    browser: InstagramBrowser | None = None
    try:
        browser = InstagramBrowser(
            proxy_string=_SMOKE_TEST_PROXY,
            user_agent=_SMOKE_TEST_USER_AGENT,
            headless=False,
        )
        browser.inject_cookies(_SMOKE_TEST_COOKIES)
        # Short config so the smoke test finishes in ~1 minute.
        result = execute_warmup(
            browser,
            args={
                "total_minutes_min": 0.6,
                "total_minutes_max": 1.2,
                "phases_min": 2,
                "phases_max": 3,
            },
        )
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
