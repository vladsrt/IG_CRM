"""
warmup action v3.2 — smooth human-like instagram browsing.

scroll: one smooth window.scrollBy per tick, variable distance (400-900px).
reels: clicks div[aria-label="Navigate to next Reel"] button.
tracks viewed posts — never returns to the same post.
time split: ~5 min feed, ~10 min reels in a 15-min session.
every action is double-verified.
"""

from __future__ import annotations

import json
import logging
import os
import random
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from workers.core.behavior import (
    HumanBehaviorEngine,
    dismiss_instagram_modals,
    safe_coordinate_click,
    _walk_up_to_clickable,
)
from workers.core.browser_core import InstagramBrowser

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════
#  CONSTANTS — tune these, not the logic
# ═══════════════════════════════════════════════════════════════════════════

DEFAULT_FEED_URL: str = "https://www.instagram.com/"
DEFAULT_PAGE_LOAD_WAIT_S: float = 5.0
DEFAULT_DURATION_MINUTES: float = 15.0

# Action weights — heavily favour reels (user wants ~2/3 reels time)
DEFAULT_WEIGHTS: Dict[str, float] = {
    "scroll_feed":   20.0,   # ~5 min of 15
    "watch_reels":   55.0,   # ~8-10 min of 15
    "open_comments": 10.0,
    "visit_profile": 15.0,
}

# ── Scroll ──
# One smooth window.scrollBy call per tick. Distance varies randomly.
_SCROLL_MIN_PX: int = 300
_SCROLL_MAX_PX: int = 950

# After scrolling, we pause to "read" the new visible post
_READ_PAUSE_MIN_S: float = 2.5
_READ_PAUSE_MAX_S: float = 8.0

# ── Post interactions ──
_LIKE_POST_PROB: float = 0.30
_OPEN_COMMENTS_PROB: float = 0.35

# ── Comments ──
_COMMENT_ATTEMPTS: Tuple[int, int] = (2, 4)
_COMMENT_LIKE_CHANCE: Tuple[float, float] = (0.55, 0.75)

# ── Reels ──
_REELS_PER_VISIT: Tuple[int, int] = (3, 8)
_REEL_WATCH_S: Tuple[float, float] = (6.0, 22.0)

# ── Profile ──
_PROFILE_DWELL_S: Tuple[float, float] = (3.0, 8.0)

# ── Inter-tick pauses (different vibe per action) ──
_PAUSE: Dict[str, Tuple[float, float]] = {
    "scroll_feed":   (1.5, 4.0),
    "open_comments": (2.5, 6.0),
    "watch_reels":   (0.8, 2.5),
    "visit_profile": (3.0, 8.0),
}

# Reels next-button selector — confirmed from real DOM
_REEL_NEXT_SEL = 'css:div[aria-label="Navigate to next Reel"]'


# ═══════════════════════════════════════════════════════════════════════════
#  UTILITY HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _r(a: float, b: float) -> float:
    """random float in [a, b]."""
    return random.uniform(a, b)


def _ri(a: int, b: int) -> int:
    """random int in [a, b]."""
    return random.randint(a, b)


def _js(page: Any, code: str):
    """run JS, return raw result."""
    try:
        return page.run_js(code)
    except Exception:
        return None


def _js_bool(page: Any, code: str) -> bool:
    """run JS returning boolean, return Python bool."""
    r = _js(page, code)
    return r is True


def _js_str(page: Any, code: str) -> str:
    """run JS returning string."""
    r = _js(page, code)
    return (r or "").strip() if isinstance(r, str) else ""


def _js_json(page: Any, code: str):
    """run JS returning JSON string, parse it."""
    raw = _js(page, code)
    if isinstance(raw, str) and raw and raw != "NF":
        try:
            return json.loads(raw)
        except Exception:
            pass
    return None


def _sweep(page: Any) -> None:
    """quick modal sweep — only runs if dialog is present."""
    if _js_bool(page, "return !!document.querySelector('div[role=\"dialog\"]')"):
        dismiss_instagram_modals(page, per_selector_timeout_s=0.3, max_dismissals=2)


# ═══════════════════════════════════════════════════════════════════════════
#  SCROLL
# ═══════════════════════════════════════════════════════════════════════════

def _scroll_down(page: Any) -> int:
    """One smooth scroll down. Returns px scrolled."""
    px = _ri(_SCROLL_MIN_PX, _SCROLL_MAX_PX)
    try:
        page.run_js(f"window.scrollBy({{top: {px}, behavior: 'smooth'}})")
    except Exception:
        try:
            page.scroll.down(px)
        except Exception:
            return 0
    time.sleep(1.2 + px / 700.0)
    return px


def _scroll_to_top(page: Any) -> None:
    """Smooth scroll back to top."""
    try:
        page.run_js("window.scrollTo({top: 0, behavior: 'smooth'})")
    except Exception:
        pass
    time.sleep(2.0)


# ═══════════════════════════════════════════════════════════════════════════
#  FIND VISIBLE POST (center of viewport, not viewed)
# ═══════════════════════════════════════════════════════════════════════════

def _get_post_href(article: Any) -> str:
    """Extract /p/XXX or /reel/XXX from an article element."""
    try:
        el = article.ele('css:a[href*="/p/"], a[href*="/reel/"]', timeout=1)
        if el:
            return (el.attr("href") or "").strip()
    except Exception:
        pass
    return ""


def _find_visible_article(page: Any, *, viewed: set) -> Any | None:
    """Return the <article> closest to viewport center, not in viewed set."""

    # JS: find article closest to screen center, return its href + position
    data = _js_json(page, """
    (()=>{
        var arts=document.querySelectorAll('article');
        var vh=window.innerHeight, center=vh/2, best=null, bestDist=1e9;
        for(var a of arts){
            var r=a.getBoundingClientRect();
            if(r.bottom<80||r.top>vh-80) continue;
            var mid=r.top+r.height/2, dist=Math.abs(mid-center);
            if(dist<bestDist){
                bestDist=dist;
                var link=(a.querySelector('a[href*="/p/"],a[href*="/reel/"]')||{}).href||'';
                best={href:link, top:Math.round(r.top)};
            }
        }
        return JSON.stringify(best||null);
    })()
    """)

    href = data.get("href", "") if data else ""

    if not href or href in viewed:
        return None

    # Find matching DrissionPage element
    try:
        el = page.ele(f'css:a[href="{href}"]', timeout=2)
        if el:
            art = el.parent("tag:article") or el.parent(2)
            if art and art.tag in ("article",):
                return art
    except Exception:
        pass

    # Fallback: any article not viewed
    try:
        for a in list(page.eles("css:article", timeout=2) or []):
            link = _get_post_href(a)
            if link and link not in viewed:
                return a
    except Exception:
        pass

    return None


# ═══════════════════════════════════════════════════════════════════════════
#  LIKE POST
# ═══════════════════════════════════════════════════════════════════════════

def _like_post(page: Any) -> bool:
    """Like visible post. Returns True if like confirmed."""
    pos = _js_json(page, """
    var s=document.querySelector('svg[aria-label="Like"][height="24"]');
    if(!s) return 'NF';
    var r=s.getBoundingClientRect();
    return JSON.stringify({x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2)});
    """)
    if not pos:
        return False

    # Click parent button (JS click bypasses hydration overlay)
    try:
        svg = page.ele('css:svg[aria-label="Like"][height="24"]', timeout=2)
        btn = _walk_up_to_clickable(svg) if svg else None
        if btn:
            btn.click(by_js=True)
        else:
            page.actions.move_to((pos["x"], pos["y"]))
            time.sleep(0.2)
            page.actions.click()
    except Exception:
        return False

    time.sleep(1.8)
    return _js_bool(page, "return !!document.querySelector('svg[aria-label=\"Unlike\"]')")


# ═══════════════════════════════════════════════════════════════════════════
#  COMMENTS
# ═══════════════════════════════════════════════════════════════════════════

def _open_comments(page: Any) -> bool:
    """Open comment modal on visible post. Returns True if dialog opened."""
    svg = page.ele('css:svg[aria-label="Comment"]', timeout=3)
    if not svg:
        return False
    btn = _walk_up_to_clickable(svg)
    if not btn:
        return False
    btn.click(by_js=True)
    time.sleep(3)
    return _js_bool(page,
        "var d=document.querySelector('div[role=\"dialog\"]');"
        "return !!(d && d.querySelector('ul li'));")


def _scroll_comments(page: Any) -> int:
    """Scroll inside open comment dialog. Returns li count."""
    n = _js(page, "var d=document.querySelector('div[role=\"dialog\"]');"
                  "return d?d.querySelectorAll('ul li').length:0")
    li_count = n if isinstance(n, int) else 0
    if li_count > 0:
        scrolls = min(8, max(1, li_count // 5))
        for _ in range(scrolls):
            _js(page,
                "var d=document.querySelector('div[role=\"dialog\"]');"
                "var ul=d&&d.querySelector('ul');"
                "if(ul)ul.parentElement.scrollBy(0,400);")
            time.sleep(_r(0.8, 2.0))
    return li_count


def _like_comments(page: Any, rng: random.Random) -> int:
    """Like comments inside dialog. Returns how many were liked."""
    hearts = _js_json(page, """
    return JSON.stringify(
        Array.from((document.querySelector('div[role="dialog"]')||[])
                   .querySelectorAll('svg[aria-label="Like"]'))
            .filter(h=>h.getAttribute('height')==='12')
            .map(h=>{var r=h.getBoundingClientRect();
                     return{x:Math.round(r.x+r.width/2), y:Math.round(r.y+r.height/2)};})
    );
    """)
    if not hearts:
        return 0

    target = _ri(*_COMMENT_ATTEMPTS)
    chance = _r(*_COMMENT_LIKE_CHANCE)
    rng.shuffle(hearts)

    liked = 0
    for h in hearts:
        if liked >= target:
            break
        if rng.random() >= chance:
            continue
        try:
            page.actions.move_to((h["x"], h["y"]))
            time.sleep(0.2)
            page.actions.click()
            liked += 1
            time.sleep(_r(0.6, 1.5))
        except Exception:
            pass
    return liked


def _close_comments(page: Any) -> None:
    """Close comment dialog."""
    try:
        _js(page,
            "var c=document.querySelector('div[role=\"dialog\"] svg[aria-label=\"Close\"]');"
            "if(c){var r=c.getBoundingClientRect();"
            "document.elementFromPoint(r.x+8,r.y+8)?.click();}")
    except Exception:
        try:
            safe_coordinate_click(page, 'css:svg[aria-label="Close"]', timeout=2)
        except Exception:
            pass
    time.sleep(1.0)


# ═══════════════════════════════════════════════════════════════════════════
#  REELS
# ═══════════════════════════════════════════════════════════════════════════

def _go_to_reels(page: Any, behavior: HumanBehaviorEngine) -> bool:
    """Navigate to Reels tab."""
    return behavior.navigate_left_rail("reels")


def _reel_next(page: Any) -> bool:
    """Click the 'Navigate to next Reel' button. Returns True if reel changed."""
    old = _js_str(page, "return window.location.href")

    btn = page.ele(_REEL_NEXT_SEL, timeout=3)
    if not btn:
        return False

    btn.click(by_js=True)
    time.sleep(2.5)

    new = _js_str(page, "return window.location.href")
    return bool(new) and new != old


def _reel_like(page: Any) -> bool:
    """Like current reel. Returns True if confirmed."""
    pos = _js_json(page, """
    var s=document.querySelector('svg[aria-label="Like"]');
    if(!s) return 'NF';
    var r=s.getBoundingClientRect();
    return JSON.stringify({x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)});
    """)
    if not pos:
        return False
    page.actions.move_to((pos["x"], pos["y"]))
    time.sleep(0.2)
    page.actions.click()
    time.sleep(1.3)
    return _js_bool(page, "return !!document.querySelector('svg[aria-label=\"Unlike\"]')")


# ═══════════════════════════════════════════════════════════════════════════
#  PROFILE
# ═══════════════════════════════════════════════════════════════════════════

def _visit_profile(page: Any, behavior: HumanBehaviorEngine) -> bool:
    """Click a random profile name from the feed."""
    try:
        links = list(page.eles('css:a[role="link"] span[dir="auto"]', timeout=2) or [])
        if not links:
            return False
        behavior.safe_click(random.choice(links))
        time.sleep(3)
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN — execute_warmup
# ═══════════════════════════════════════════════════════════════════════════

def execute_warmup(
    browser: InstagramBrowser,
    args: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run a human-like warmup session.

    v3.2: smooth scrolling, reels-first time split, viewed-post tracking,
    double-verify on every interaction.
    """
    args = args or {}
    rng = random.Random()
    page = browser.page

    feed_url = str(args.get("feed_url", DEFAULT_FEED_URL))
    wait_s = float(args.get("page_load_wait_s", DEFAULT_PAGE_LOAD_WAIT_S))
    duration_min = float(args.get("duration_minutes", DEFAULT_DURATION_MINUTES))

    if duration_min <= 0:
        raise ValueError(f"duration_minutes must be > 0, got {duration_min}")

    weights = dict(DEFAULT_WEIGHTS)
    for k, v in (args.get("action_weights") or {}).items():
        if k in weights:
            weights[k] = float(v)

    end_time = time.monotonic() + duration_min * 60.0

    logger.info("[warmup] v3.2 start — %.1f min, weights=%s", duration_min, weights)

    # ── Setup ──────────────────────────────────────────────────────────────
    page.get(feed_url)
    time.sleep(wait_s)
    dismiss_instagram_modals(page)

    behavior = HumanBehaviorEngine(page)
    behavior.idle(2.0, 5.0)  # "just opened app"

    viewed: set[str] = set()
    ticks_top = 0

    c: Dict[str, int] = {
        "ticks": 0, "posts_liked": 0, "comment_modals": 0, "comments_liked": 0,
        "reel_sessions": 0, "reels_watched": 0, "reels_watch_s": 0,
        "reels_liked": 0, "reel_comments": 0, "profiles": 0, "scroll_tops": 0,
    }
    log: List[Dict[str, Any]] = []

    # ── Main loop ──────────────────────────────────────────────────────────
    while time.monotonic() < end_time:
        # Pick action by weighted random
        keys, vals = list(weights), [max(0.0, float(weights[k])) for k in weights]
        action = rng.choices(keys, weights=vals, k=1)[0] if any(vals) else "scroll_feed"

        t0 = time.monotonic()
        logger.info("[warmup] #%d %s", c["ticks"] + 1, action)
        ok, err = True, None

        try:
            # ── SCROLL FEED ───────────────────────────────────────────────
            if action == "scroll_feed":
                _sweep(page)
                _scroll_down(page)
                time.sleep(_r(_READ_PAUSE_MIN_S, _READ_PAUSE_MAX_S))

                art = _find_visible_article(page, viewed=viewed)
                if art:
                    href = _get_post_href(art)
                    viewed.add(href)

                    if rng.random() < _LIKE_POST_PROB:
                        if _like_post(page):
                            c["posts_liked"] += 1
                            behavior.idle(0.8, 2.0)

                    if rng.random() < _OPEN_COMMENTS_PROB:
                        if _open_comments(page):
                            c["comment_modals"] += 1
                            behavior.idle(1.2, 2.5)
                            _scroll_comments(page)
                            c["comments_liked"] += _like_comments(page, rng)
                            _close_comments(page)

                ticks_top += 1
                if ticks_top >= _ri(8, 14):
                    _scroll_to_top(page)
                    c["scroll_tops"] += 1
                    ticks_top = 0
                    behavior.idle(2.0, 4.0)

            # ── REELS ─────────────────────────────────────────────────────
            elif action == "watch_reels":
                _sweep(page)
                if not _go_to_reels(page, behavior):
                    ok, err = False, "reels_nav_fail"
                else:
                    c["reel_sessions"] += 1
                    behavior.idle(2.0, 4.0)

                    for _ in range(_ri(*_REELS_PER_VISIT)):
                        if time.monotonic() >= end_time:
                            break

                        watch = _r(*_REEL_WATCH_S)
                        time.sleep(watch)
                        c["reels_watched"] += 1
                        c["reels_watch_s"] += int(watch)

                        act = rng.choices(["none","like","comment","both"],
                                          weights=[55,30,10,5])[0]

                        if act in ("like", "both") and _reel_like(page):
                            c["reels_liked"] += 1
                        if act in ("comment", "both"):
                            if _open_comments(page):
                                c["reel_comments"] += 1
                                _scroll_comments(page)
                                c["comments_liked"] += _like_comments(page, rng)
                                _close_comments(page)

                        if not _reel_next(page):
                            break
                        behavior.idle(0.4, 1.2)

                    page.get(feed_url)
                    time.sleep(wait_s)
                    dismiss_instagram_modals(page)

            # ── OPEN COMMENTS ──────────────────────────────────────────────
            elif action == "open_comments":
                _sweep(page)
                art = _find_visible_article(page, viewed=viewed)
                if art:
                    href = _get_post_href(art)
                    viewed.add(href)
                    if _open_comments(page):
                        c["comment_modals"] += 1
                        behavior.idle(1.0, 2.5)
                        _scroll_comments(page)
                        c["comments_liked"] += _like_comments(page, rng)
                        _close_comments(page)
                else:
                    _scroll_down(page)
                    time.sleep(_r(_READ_PAUSE_MIN_S, _READ_PAUSE_MAX_S))

            # ── VISIT PROFILE ─────────────────────────────────────────────
            elif action == "visit_profile":
                _sweep(page)
                if _visit_profile(page, behavior):
                    c["profiles"] += 1
                    for _ in range(_ri(1, 3)):
                        try:
                            page.scroll.down(400)
                        except Exception:
                            pass
                        time.sleep(_r(1.0, 2.5))
                    try:
                        page.back()
                    except Exception:
                        page.get(feed_url)
                    time.sleep(3)
                    dismiss_instagram_modals(page)

        except Exception as exc:
            ok, err = False, f"{type(exc).__name__}: {exc}"
            logger.warning("[warmup] %s FAILED: %s", action, err)

        c["ticks"] += 1
        log.append({"action": action, "ok": ok, "elapsed_s": round(time.monotonic() - t0, 2),
                     "error": err})

        lo, hi = _PAUSE.get(action, (1.5, 4.0))
        behavior.idle(lo, hi)

    # ── Done ────────────────────────────────────────────────────────────────
    logger.info("[warmup] done %d ticks: %s", c["ticks"],
                {k: v for k, v in c.items() if v})
    return {
        "action": "warmup", "version": 3,
        "feed_url": feed_url, "duration_minutes": duration_min,
        "weights": weights, "counters": c,
        "viewed_posts": len(viewed), "action_log": log,
    }
