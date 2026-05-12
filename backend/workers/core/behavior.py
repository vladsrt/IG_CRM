"""Human-like behavior engine.

One place to emulate a human user for DrissionPage. We do not touch the DOM
directly and we never teleport the cursor in 0ms, because Instagram flags
that as bot activity. Instead we use bezier mouse paths, variable typing
speed, and human-like pauses.
"""

from __future__ import annotations

import logging
import math
import random
import time
from typing import Any, Dict, Iterable, Optional, Protocol, Tuple, Union

logger = logging.getLogger(__name__)


# type aliases
Coord = Tuple[int, int]
"""Absolute (x, y) cursor coord in viewport pixels."""

Locator = Union["_HasRect", Coord]
"""What the engine takes as a target: a DrissionPage element OR a coord."""


class _HasRect(Protocol):
    """Structural protocol for DrissionPage element handles.

    A Protocol (not a real import) so this module still imports when
    DrissionPage is not installed (CI, unit tests, typecheck-only runs).
    """

    rect: Any  # DrissionPage gives .rect.midpoint as an (x, y) tuple

    def click(self) -> Any: ...
    def input(self, text: str, clear: bool = ...) -> Any: ...


# knobs
_DEFAULT_OVERSHOOT_PROBABILITY: float = 0.18
_DEFAULT_OVERSHOOT_PIXELS: Tuple[int, int] = (8, 22)
_DEFAULT_STEP_COUNT_RANGE: Tuple[int, int] = (22, 42)
_DEFAULT_BASE_STEP_DELAY_S: float = 0.011
_DEFAULT_FAST_STEP_DELAY_S: float = 0.005
_DEFAULT_KEYSTROKE_RANGE_S: Tuple[float, float] = (0.055, 0.18)
_DEFAULT_THINKING_PROBABILITY: float = 0.05
_DEFAULT_THINKING_RANGE_S: Tuple[float, float] = (0.45, 1.2)
_READING_CHARS_PER_SECOND: float = 22.0  # ~250wpm * 5 chars/word / 60s
_READING_MIN_S: float = 0.6
_READING_MAX_S: float = 12.0
_MICRO_SCROLL_COUNT_RANGE: Tuple[int, int] = (1, 3)
_MICRO_SCROLL_PIXEL_RANGE: Tuple[int, int] = (60, 240)
_MICRO_SCROLL_PAUSE_RANGE_S: Tuple[float, float] = (0.25, 0.75)


class HumanBehaviorEngine:
    """Adds human-like timing to a DrissionPage ChromiumPage session.

    One engine per browser session is the normal pattern. Build it inside
    an action handler and do not pass it around. Engines keep no shared
    state across handlers except the current cursor position.
    """

    def __init__(
        self,
        page: Any,
        *,
        overshoot_probability: float = _DEFAULT_OVERSHOOT_PROBABILITY,
        step_count_range: Tuple[int, int] = _DEFAULT_STEP_COUNT_RANGE,
        rng_seed: Optional[int] = None,
    ) -> None:
        """Set up the engine.

        Args:
            page: DrissionPage ChromiumPage owned by an InstagramBrowser.
            overshoot_probability: chance that click() will overshoot the
                target by a few pixels and then correct. Must be in [0, 1].
            step_count_range: inclusive (min, max) number of points along
                each bezier curve. Higher = smoother but slower paths.
            rng_seed: optional seed so tests are deterministic. In prod
                pass None.
        """
        if not 0.0 <= overshoot_probability <= 1.0:
            raise ValueError(
                f"overshoot_probability must be in [0,1], got {overshoot_probability}"
            )
        lo, hi = step_count_range
        if lo < 2 or hi < lo:
            raise ValueError(f"Invalid step_count_range: {step_count_range}")

        self._page = page
        self._overshoot_probability = overshoot_probability
        self._step_count_range = step_count_range
        self._rng = random.Random(rng_seed)
        self._cursor: Coord = (0, 0)

    # public api
    def move_to(self, target: Locator) -> Coord:
        """Move the cursor to `target` along a sampled bezier curve.

        Returns the final cursor coord. Never teleports. May overshoot and
        correct. Safe to call when the cursor is already near the target,
        the curve just becomes a short, low-step path.
        """
        target_xy = self._coord_of(target)

        if self._rng.random() < self._overshoot_probability:
            ox = target_xy[0] + self._rng.randint(
                -_DEFAULT_OVERSHOOT_PIXELS[1], _DEFAULT_OVERSHOOT_PIXELS[1]
            )
            oy = target_xy[1] + self._rng.randint(
                -_DEFAULT_OVERSHOOT_PIXELS[1], _DEFAULT_OVERSHOOT_PIXELS[1]
            )
            self._draw_bezier(self._cursor, (ox, oy), fast=False)
            self.idle(0.06, 0.14)  # tiny pause before the correction
            self._draw_bezier((ox, oy), target_xy, fast=True)
        else:
            self._draw_bezier(self._cursor, target_xy, fast=False)

        self._cursor = target_xy
        return target_xy

    def click(self, target: Locator) -> None:
        """Move to `target` then click via DrissionPage Actions.

        We use page.actions.click() on purpose: it sends a synthetic
        mousedown/mouseup pair AT the current cursor position. ele.click()
        would jump to the element center with no path, killing the
        trajectory we just drew.
        """
        self.move_to(target)
        self.idle(0.05, 0.18)
        try:
            self._page.actions.click()
        except Exception as exc:
            logger.debug("[behavior] actions.click failed (%s), fallback to ele.click", exc)
            if hasattr(target, "click"):
                target.click()  # type: ignore[union-attr]
            else:
                raise

    def hover_then_click(
        self,
        target: _HasRect,
        *,
        hover_dwell_range_s: Tuple[float, float] = (0.5, 1.0),
        click_after: bool = True,
    ) -> bool:
        """Hover the element, wait for React to hydrate, then click.

        IG's left nav rail attaches click handlers only after the first
        mouseenter. Direct clicks on unhydrated elements usually fail. We
        hover first, wait a bit, then click. Just like a real user.

        Args:
            target: DrissionPage element to hover.
            hover_dwell_range_s: how long to dwell before clicking, so the
                element can hydrate.
            click_after: if False, skip the click (useful when the target
                changes on hover).

        Returns:
            bool: True if hover and (if used) click both worked.
        """
        if hover_dwell_range_s[0] < 0 or hover_dwell_range_s[1] < hover_dwell_range_s[0]:
            raise ValueError(
                f"Invalid hover_dwell_range_s: {hover_dwell_range_s}"
            )

        # step 1: bezier-move the cursor to the element so mouseenter
        # arrives along a real path.
        try:
            self.move_to(target)
        except Exception as exc:
            logger.warning("[behavior] hover_then_click move_to failed (%s)", exc)

        # step 2: fire the explicit hover event. DrissionPage ele.hover()
        # sends a real mouseenter/mouseover pair, which the React rail
        # listens for.
        try:
            target.hover()  # type: ignore[union-attr]
        except Exception as exc:
            logger.debug(
                "[behavior] target.hover() failed (%s), using move_to mouseenter",
                exc,
            )

        # step 3: wait for the rail to hydrate
        self.idle(*hover_dwell_range_s)

        if not click_after:
            return True

        # step 4: click. Routed through actions.click() so we keep
        # the cursor trajectory we just drew.
        try:
            self._page.actions.click()
            return True
        except Exception as exc:
            logger.warning(
                "[behavior] hover_then_click actions.click failed (%s), "
                "fallback to ele.click",
                exc,
            )
            try:
                target.click()  # type: ignore[union-attr]
                return True
            except Exception as exc2:
                logger.error(
                    "[behavior] hover_then_click ele.click also failed (%s)", exc2
                )
                return False

    def dismiss_interruptions(
        self,
        *,
        per_selector_timeout_s: float = 1.0,
        max_dismissals: int = 3,
        enable_corner_click_fallback: bool = True,
        enable_escape_fallback: bool = True,
    ) -> int:
        """Soft sweep for IG modals (notifications, home prompts, etc).

        We use text and aria-based selectors because class hashes change
        often. Fails softly, so the main flow does not crash if there are
        no modals at all.

        Args:
            per_selector_timeout_s: how long to wait per selector check.
            max_dismissals: how many sweep retries we do in case modals stack.
            enable_corner_click_fallback: try clicking the top-left corner
                to close.
            enable_escape_fallback: try sending the ESC key.

        Returns:
            int: how many modals we closed.
        """
        if max_dismissals < 1:
            raise ValueError(f"max_dismissals must be >= 1, got {max_dismissals}")
        if per_selector_timeout_s <= 0:
            raise ValueError(
                f"per_selector_timeout_s must be > 0, got {per_selector_timeout_s}"
            )

        # delegate to the module-level helper, so the 3-layer defense
        # (text button sweep, backdrop click at (10, 10), ESC key) lives
        # in one place. This method exists mostly so action handlers that
        # already have a behavior engine in scope can call
        # self.dismiss_interruptions() inline.
        return dismiss_instagram_modals(
            self._page,
            per_selector_timeout_s=per_selector_timeout_s,
            max_dismissals=max_dismissals,
            enable_corner_click_fallback=enable_corner_click_fallback,
            enable_escape_fallback=enable_escape_fallback,
        )

    # --- Left-rail navigation ---
    # Selector pool per target. Each target lists fallbacks in priority
    # order. The svg[aria-label=...] form is the icon at the rail's
    # collapsed state; the span[normalize-space()=...] form is the
    # label that appears once the rail expands on hover. Either one
    # matched is enough.
    # IG's React router refuses to honor visual-only clicks on the bare
    # ``<svg>`` icons for Reels (and increasingly other rail entries) —
    # the click handler is bound to the wrapping ``<a>``, so clicking
    # the SVG just hits an invisible hydration overlay. We list the
    # parent ``<a>`` selector FIRST for every target so the resolver
    # picks the link element, and fall back to the SVG / span only if
    # the link isn't in the DOM yet. Combined with ``ele.click(by_js=True)``
    # in :meth:`navigate_left_rail`, this is the only pattern that
    # consistently survives the overlay.
    _LEFT_RAIL_TARGETS: Dict[str, list[str]] = {
        "create": [
            'xpath://a[contains(@href,"/create/")]',
            'css:svg[aria-label="New post"]',
            'xpath://nav//span[normalize-space()="Create"]',
            'xpath://*[@role="link" or @role="button"][.//span[normalize-space()="Create"]]',
        ],
        "reels": [
            # Parent <a> first — React router only listens here.
            'xpath://a[contains(@href,"/reels/")]',
            'css:svg[aria-label="Reels"]',
            'xpath://nav//span[normalize-space()="Reels"]',
        ],
        "home": [
            'xpath://nav//a[@href="/"]',
            'css:svg[aria-label="Home"]',
            'xpath://nav//span[normalize-space()="Home"]',
        ],
        "profile": [
            'xpath://nav//a[@role="link"][.//img[contains(@alt," profile picture")]]',
            'css:nav img[alt$=" profile picture"]',
            'xpath://nav//span[normalize-space()="Profile"]',
        ],
        "explore": [
            'xpath://a[contains(@href,"/explore/")]',
            'css:svg[aria-label="Explore"]',
            'xpath://nav//span[normalize-space()="Explore"]',
        ],
        "search": [
            'css:svg[aria-label="Search"]',
            'xpath://nav//span[normalize-space()="Search"]',
        ],
    }

    # Selectors for the "rail anchor" we hover first so the rail
    # hydrates / expands. Home's icon is the single most reliable
    # element across every IG variant — it's always present and
    # always the same aria-label.
    _LEFT_RAIL_ANCHORS: list[str] = [
        'css:nav[role="navigation"]',
        'css:svg[aria-label="Home"]',
        'css:nav svg',
        'xpath://nav',
    ]

    def navigate_left_rail(
        self,
        target: str,
        *,
        hover_dwell_s: float = 1.0,
        find_timeout_s: float = 6.0,
    ) -> bool:
        """Navigate left rail using hover hydration and JS clicks.

        React router ignores basic clicks on SVG icons here due to an invisible 
        hydration overlay. We hover the rail anchor (Home) to expand it, 
        then trigger a JS click directly on the target element to bypass the overlay.

        Args:
            target: "create", "reels", "home", "profile", "explore", or "search".
            hover_dwell_s: Wait time for React handlers to mount.
            find_timeout_s: Max time to find the target.

        Returns:
            bool: True if click was successful.
        """
        if hover_dwell_s < 0:
            raise ValueError(f"hover_dwell_s must be >= 0, got {hover_dwell_s}")

        key = (target or "").strip().lower()
        if key not in self._LEFT_RAIL_TARGETS:
            raise ValueError(
                f"unknown left-rail target {target!r}; expected one of "
                f"{sorted(self._LEFT_RAIL_TARGETS)}"
            )

        # Step 1 — locate a rail anchor and hover it.
        anchor: Any = None
        for sel in self._LEFT_RAIL_ANCHORS:
            try:
                anchor = self._page.ele(sel, timeout=1.0)
            except Exception as exc:
                logger.debug(
                    "[behavior] navigate_left_rail anchor %r raised (%s)", sel, exc,
                )
                continue
            if anchor:
                logger.debug(
                    "[behavior] navigate_left_rail using anchor %r", sel,
                )
                break
        if anchor is None:
            logger.warning(
                "[behavior] navigate_left_rail: no rail anchor found; the rail "
                "may not be in the DOM yet — proceeding without hover hydration"
            )
        else:
            try:
                self.move_to(anchor)
            except Exception as exc:
                logger.debug(
                    "[behavior] navigate_left_rail move_to anchor failed (%s)", exc,
                )
            try:
                anchor.hover()  # explicit mouseenter for React
            except Exception as exc:
                logger.debug(
                    "[behavior] navigate_left_rail anchor.hover failed (%s)", exc,
                )

        # Step 2 — dwell so the rail's React state finishes hydrating
        # and any expansion animation completes.
        time.sleep(max(0.0, hover_dwell_s))

        # Step 3 — find the actual target button using the per-key
        # selector pool, then safe_click it.
        target_ele: Any = None
        chosen_sel: Optional[str] = None
        for sel in self._LEFT_RAIL_TARGETS[key]:
            try:
                target_ele = self._page.ele(sel, timeout=find_timeout_s / len(self._LEFT_RAIL_TARGETS[key]))
            except Exception as exc:
                logger.debug(
                    "[behavior] navigate_left_rail target %r raised (%s)", sel, exc,
                )
                continue
            if target_ele:
                chosen_sel = sel
                break

        if target_ele is None:
            logger.error(
                "[behavior] navigate_left_rail %r: none of %s matched",
                key, self._LEFT_RAIL_TARGETS[key],
            )
            return False

        # --- CRITICAL: resolve SVG → wrapping button BEFORE any JS click. ---
        # SVGs don't inherit HTMLElement.click(), so ``ele.click(by_js=True)``
        # on a ``<svg aria-label="New post">`` throws
        # ``TypeError: this.click is not a function``. ``_ensure_clickable``
        # walks up the tree to the nearest ``<a>`` / ``<button>`` /
        # ``[role="button"]`` ancestor — which is what IG's React router
        # actually listens on anyway.
        clickable = _ensure_clickable(target_ele)
        if clickable is not target_ele:
            logger.info(
                "[behavior] navigate_left_rail %r: walked SVG → clickable parent",
                key,
            )

        logger.info(
            "[behavior] navigate_left_rail %r: target resolved via %r — "
            "preparing JS click",
            key, chosen_sel,
        )

        # Step 4a — hover the resolved clickable so the wrapping <a>'s
        # React handler is fully wired.
        try:
            clickable.hover()
        except Exception as exc:
            logger.debug(
                "[behavior] navigate_left_rail %r: clickable.hover() failed (%s); "
                "proceeding to JS click anyway",
                key, exc,
            )
        time.sleep(1.0)

        # Step 4b — JS click on the wrapping button (NOT the SVG). This
        # dispatches the click via element.click() in page context,
        # bypassing the invisible hydration overlay that swallows
        # coordinate clicks. Safe to call by_js here because
        # _ensure_clickable guaranteed we're not pointing at an SVG.
        clicked = False
        try:
            # Last-line guard: if the resolver returned the original SVG
            # (no walkable parent existed) we MUST NOT use by_js — fall
            # through to the coordinate-click branch instead.
            tag = (clickable.tag or "").lower() if clickable is not None else ""
            if tag == "svg":
                raise TypeError(
                    "resolved element is still an <svg> — cannot use JS click"
                )
            clickable.click(by_js=True)
            clicked = True
            logger.info(
                "[behavior] navigate_left_rail %r: JS click dispatched", key,
            )
        except Exception as exc:
            logger.warning(
                "[behavior] navigate_left_rail %r: ele.click(by_js=True) failed "
                "(%s); falling back to safe_click",
                key, exc,
            )
            # Fallback chain — humanized cursor click first, then the
            # scroll-into-view variant for clipped icons.
            clicked = self.safe_click(clickable)
            if not clicked:
                logger.warning(
                    "[behavior] navigate_left_rail %r: safe_click failed; "
                    "trying safe_click_button as last resort",
                    key,
                )
                try:
                    self.safe_click_button(clickable)
                    clicked = True
                except Exception as exc2:
                    logger.error(
                        "[behavior] navigate_left_rail %r: every click variant "
                        "failed (last error: %s)",
                        key, exc2,
                    )

        if clicked:
            # Tiny dwell so any nav-induced page change has a chance to
            # start before the caller queries the new DOM.
            self.idle(0.6, 1.4)
        return clicked

    def safe_click_button(
        self,
        target: _HasRect,
        *,
        settle_s: float = 1.0,
        pre_click_pause_range_s: Tuple[float, float] = (0.4, 0.9),
    ) -> None:
        """Scroll element into center viewport, let layout settle, then click.

        Useful for commit buttons (Submit, Share) that might be clipped 
        by sticky headers. Scrolling to center ensures coordinate clicks 
        actually land on the target.

        Args:
            target: Element to click.
            settle_s: Wait time for React reflow after scroll.
            pre_click_pause_range_s: Human hesitation before clicking.
        """
        try:
            target.scroll.to_see(center=True)  # type: ignore[union-attr]
        except Exception as exc:
            logger.debug(
                "[behavior] scroll.to_see(center=True) failed (%s); proceeding without",
                exc,
            )
        # Settle the layout — IG's sticky header sometimes overshoots the
        # scroll target, and the safest fix is just to wait a beat.
        time.sleep(max(0.0, settle_s))
        self.idle(*pre_click_pause_range_s)
        self.click(target)

    def clear_input_field(
        self,
        target: _HasRect,
        *,
        focus_first: bool = True,
        backspace_passes: int = 1,
    ) -> bool:
        """Clear an input field via JS injection and event dispatching.

        React ignores hardware backspaces if state isn't synced. We update 
        node values directly and fire 'input' events so React state catches up.

        Args:
            target: The input/textarea element.
            focus_first: Click to focus before wiping.
            backspace_passes: Retries for the keyboard Ctrl+A fallback.

        Returns:
            bool: True if field is empty at the end.
        """
        if backspace_passes < 1:
            raise ValueError(f"backspace_passes must be >= 1, got {backspace_passes}")

        if focus_first:
            self.click(target)
            self.idle(0.18, 0.45)

        # --- Step 1 — JS wipe on the element handle ---
        js_wipe = (
            "this.value = '';"
            "this.textContent = '';"
            "this.innerText = '';"
            "this.dispatchEvent(new Event('input',  { bubbles: true }));"
            "this.dispatchEvent(new Event('change', { bubbles: true }));"
        )
        wiped = False
        try:
            target.run_js(js_wipe)  # type: ignore[union-attr]
            wiped = True
            logger.info("[behavior] clear_input_field: JS wipe dispatched on element")
        except Exception as exc:
            logger.warning(
                "[behavior] clear_input_field: element-level run_js failed (%s); "
                "trying page-level injection on document.activeElement",
                exc,
            )
            # Some DrissionPage versions don't expose run_js on the
            # element handle. Fall through to a page-level eval that
            # operates on document.activeElement (the field we just
            # focused above).
            try:
                self._page.run_js(
                    "var el = document.activeElement;"
                    "if (el) {"
                    "  el.value = '';"
                    "  el.textContent = '';"
                    "  el.innerText = '';"
                    "  el.dispatchEvent(new Event('input',  { bubbles: true }));"
                    "  el.dispatchEvent(new Event('change', { bubbles: true }));"
                    "}"
                )
                wiped = True
                logger.info("[behavior] clear_input_field: page-level JS wipe dispatched")
            except Exception as exc2:
                logger.warning(
                    "[behavior] clear_input_field: page-level run_js also failed (%s)",
                    exc2,
                )

        self.idle(0.12, 0.28)

        # --- Step 2 — verify empty ---
        residual: str = ""
        if wiped:
            try:
                raw = target.run_js(  # type: ignore[union-attr]
                    "return (this.value || '') + (this.textContent || '');"
                )
                residual = (raw or "").strip()
            except Exception as exc:
                logger.debug(
                    "[behavior] clear_input_field: residual read failed (%s); "
                    "assuming success",
                    exc,
                )
                residual = ""

        # step 3: use backspace if still not empty
        if residual:
            logger.warning(
                "[behavior] clear_input_field: %d chars remained after JS wipe; "
                "falling back to Ctrl+A + Backspace",
                len(residual),
            )
            for pass_idx in range(max(1, backspace_passes)):
                try:
                    actions = self._page.actions
                    actions.key_down("ctrl")
                    self.idle(0.04, 0.12)
                    actions.type("a")
                    self.idle(0.04, 0.12)
                    actions.key_up("ctrl")
                except Exception as exc:
                    logger.debug(
                        "[behavior] fallback Ctrl+A failed (%s) on pass %d",
                        exc, pass_idx,
                    )
                self.idle(0.08, 0.20)
                try:
                    self._page.actions.type("\b")  # \b = Backspace
                except Exception as exc:
                    logger.debug(
                        "[behavior] fallback Backspace failed (%s) on pass %d",
                        exc, pass_idx,
                    )
                self.idle(0.10, 0.24)

            try:
                raw = target.run_js(  # type: ignore[union-attr]
                    "return (this.value || '') + (this.textContent || '');"
                )
                residual = (raw or "").strip()
            except Exception:
                residual = ""

        success = not residual
        if success:
            logger.info("[behavior] clear_input_field: field wiped clean")
        else:
            logger.error(
                "[behavior] clear_input_field: %d chars STILL present after "
                "JS + keystroke fallbacks",
                len(residual),
            )
        return success

    def type_into(
        self,
        target: _HasRect,
        text: str,
        *,
        focus_first: bool = True,
        char_delay_range_s: Tuple[float, float] = _DEFAULT_KEYSTROKE_RANGE_S,
        thinking_probability: float = _DEFAULT_THINKING_PROBABILITY,
    ) -> None:
        """
        types text into target one by one.
        adds random delays between keys to look real.
        """
        if focus_first:
            self.click(target)
            self.idle(0.25, 0.7)

        lo, hi = char_delay_range_s
        if lo < 0 or hi < lo:
            raise ValueError(f"Invalid char_delay_range_s: {char_delay_range_s}")

        for ch in text:
            try:
                target.input(ch, clear=False)
            except TypeError:
                # Some DrissionPage versions don't accept the clear kwarg.
                target.input(ch)  # type: ignore[call-arg]
            time.sleep(self._rng.uniform(lo, hi))
            if self._rng.random() < thinking_probability:
                time.sleep(self._rng.uniform(*_DEFAULT_THINKING_RANGE_S))

    def read_pause(self, content_length: Optional[int] = None) -> None:
        """
        wait to simulate reading text.
        """
        if content_length is None or content_length <= 0:
            time.sleep(self._rng.uniform(_READING_MIN_S, 1.8))
            return
        estimated = content_length / _READING_CHARS_PER_SECOND
        jittered = self._rng.uniform(estimated * 0.7, estimated * 1.3)
        time.sleep(max(_READING_MIN_S, min(jittered, _READING_MAX_S)))

    def micro_scroll(
        self,
        *,
        count_range: Tuple[int, int] = _MICRO_SCROLL_COUNT_RANGE,
        pixel_range: Tuple[int, int] = _MICRO_SCROLL_PIXEL_RANGE,
        upward_probability: float = 0.3,
    ) -> None:
        """
        scrolls a little bit up or down.
        """
        n_lo, n_hi = count_range
        n = self._rng.randint(n_lo, n_hi)
        for _ in range(n):
            delta = self._rng.randint(*pixel_range)
            try:
                if self._rng.random() < upward_probability:
                    self._page.scroll.up(max(20, delta // 2))
                else:
                    self._page.scroll.down(delta)
            except Exception as exc:
                logger.debug("[behavior] scroll failed (%s); skipping", exc)
                return
            time.sleep(self._rng.uniform(*_MICRO_SCROLL_PAUSE_RANGE_S))

    def idle(self, min_s: float, max_s: float) -> None:
        """sleep for a random time between min_s and max_s."""
        if min_s < 0 or max_s < min_s:
            raise ValueError(f"Invalid idle range: ({min_s}, {max_s})")
        time.sleep(self._rng.uniform(min_s, max_s))

    # js smooth scroll
    def smooth_scroll(
        self,
        min_y: int = 300,
        max_y: int = 800,
        *,
        upward_probability: float = 0.15,
        post_scroll_pause_range_s: Tuple[float, float] = (0.8, 2.6),
    ) -> int:
        """
        scrolls the page smoothly using javascript.
        """
        if min_y < 1 or max_y < min_y:
            raise ValueError(f"Invalid smooth_scroll bounds: ({min_y}, {max_y})")
        if not 0.0 <= upward_probability <= 1.0:
            raise ValueError(
                f"upward_probability out of [0,1]: {upward_probability}"
            )

        magnitude = self._rng.randint(min_y, max_y)
        direction = -1 if self._rng.random() < upward_probability else 1
        # Upward corrections are usually shorter than the original scroll,
        # because a real user nudges back, not flicks all the way.
        if direction == -1:
            magnitude = max(80, magnitude // 2)

        y_offset = direction * magnitude
        try:
            self._page.run_js(
                f"window.scrollBy({{top: {y_offset}, behavior: 'smooth'}});"
            )
        except Exception as exc:
            # JS injection failed (rare — usually a detached frame mid-nav).
            # Fall back to DrissionPage's scroll so the session keeps moving.
            logger.debug(
                "[behavior] run_js smooth scroll failed (%s); using fallback",
                exc,
            )
            try:
                if direction == 1:
                    self._page.scroll.down(magnitude)
                else:
                    self._page.scroll.up(magnitude)
            except Exception as exc2:
                logger.debug(
                    "[behavior] fallback scroll also failed (%s); skipping tick",
                    exc2,
                )
                return 0

        # Pause for layout + reading. Always non-zero — back-to-back wheel
        # events with no settle are themselves a bot signal.
        self.idle(*post_scroll_pause_range_s)
        return y_offset

    def safe_click(
        self,
        target: Any,
        *,
        hover_first: bool = True,
        pre_click_pause_range_s: Tuple[float, float] = (0.2, 0.7),
    ) -> bool:
        """
        clicks an element safely, ignoring errors if not found.
        """
        if target is None:
            return False

        try:
            if hover_first and hasattr(target, "rect"):
                try:
                    self.move_to(target)
                except Exception as exc:
                    logger.debug("[behavior] safe_click hover failed (%s)", exc)

            self.idle(*pre_click_pause_range_s)

            # Prefer the trajectory-preserving actions.click() path; if
            # that fails, fall back to the element's own click().
            try:
                self._page.actions.click()
                return True
            except Exception as exc:
                logger.debug(
                    "[behavior] safe_click actions.click failed (%s); "
                    "falling back to ele.click",
                    exc,
                )
                if hasattr(target, "click"):
                    try:
                        target.click()
                        return True
                    except Exception as exc2:
                        logger.debug(
                            "[behavior] safe_click ele.click also failed (%s)",
                            exc2,
                        )
        except Exception as exc:
            # Defensive catch-all — safe_click must never throw.
            logger.debug("[behavior] safe_click outer guard caught (%s)", exc)
        return False

    # deep humanization helpers
    def deep_scroll_session(
        self,
        *,
        duration_s: float,
        upward_correction_probability: float = 0.18,
        flick_probability: float = 0.12,
    ) -> Dict[str, int]:
        """
        scrolls the page for a given duration.
        """
        if duration_s <= 0:
            raise ValueError(f"duration_s must be > 0, got {duration_s}")
        if not 0.0 <= upward_correction_probability <= 1.0:
            raise ValueError(
                f"upward_correction_probability out of [0,1]: {upward_correction_probability}"
            )
        if not 0.0 <= flick_probability <= 1.0:
            raise ValueError(f"flick_probability out of [0,1]: {flick_probability}")

        counters: Dict[str, int] = {
            "slow_reads": 0, "skims": 0, "flicks": 0, "upward_corrections": 0,
        }
        deadline = time.monotonic() + duration_s

        while time.monotonic() < deadline:
            roll = self._rng.random()
            try:
                if roll < upward_correction_probability:
                    self._page.scroll.up(self._rng.randint(180, 520))
                    self.idle(0.6, 1.7)
                    counters["upward_corrections"] += 1
                elif roll < upward_correction_probability + flick_probability:
                    bursts = self._rng.randint(3, 6)
                    for _ in range(bursts):
                        self._page.scroll.down(self._rng.randint(420, 880))
                        time.sleep(self._rng.uniform(0.05, 0.18))
                    self.idle(0.4, 1.1)
                    counters["flicks"] += 1
                elif self._rng.random() < 0.55:
                    # Slow read — short scroll, long dwell
                    self._page.scroll.down(self._rng.randint(160, 360))
                    # Imagine a 200-1500 char post and "read" it
                    self.read_pause(content_length=self._rng.randint(200, 1500))
                    counters["slow_reads"] += 1
                else:
                    # Skim — medium scroll, medium dwell
                    self._page.scroll.down(self._rng.randint(280, 560))
                    self.idle(1.2, 3.4)
                    counters["skims"] += 1
            except Exception as exc:
                # Don't kill the whole session on one bad scroll — log and
                # back off briefly. Common cause: page is in a transient
                # navigation that briefly detaches the scroll target.
                logger.debug("[behavior] deep_scroll tick failed (%s); backing off", exc)
                time.sleep(self._rng.uniform(0.5, 1.4))

        return counters

    def maybe_like_visible_post(
        self,
        *,
        like_probability: float = 0.25,
    ) -> bool:
        """
        likes a post on the screen based on probability.
        """
        if not 0.0 <= like_probability <= 1.0:
            raise ValueError(f"like_probability out of [0,1]: {like_probability}")
        if self._rng.random() >= like_probability:
            return False

        like_btn = self._first_visible_element(
            [
                'css:section svg[aria-label="Like"]',
                'xpath://section//*[@aria-label="Like" and @role="img"]',
                'xpath://*[@role="button"]//*[@aria-label="Like"]',
            ]
        )
        if like_btn is None:
            return False

        try:
            self.safe_click_button(like_btn, settle_s=0.4, pre_click_pause_range_s=(0.6, 1.2))
        except Exception as exc:
            logger.debug("[behavior] maybe_like click failed (%s)", exc)
            return False

        # Tiny dwell — humans don't immediately scroll past a post they liked.
        self.idle(0.7, 1.6)
        return True

    def browse_comments(
        self,
        *,
        open_probability: float = 0.35,
        like_count_range: Tuple[int, int] = (0, 2),
        read_seconds_range: Tuple[float, float] = (3.0, 9.0),
    ) -> Dict[str, int]:
        """
        opens comments and maybe likes some.
        """
        if not 0.0 <= open_probability <= 1.0:
            raise ValueError(f"open_probability out of [0,1]: {open_probability}")
        lk_lo, lk_hi = like_count_range
        if lk_lo < 0 or lk_hi < lk_lo:
            raise ValueError(f"Invalid like_count_range: {like_count_range}")

        result: Dict[str, int] = {"opened": 0, "comments_liked": 0, "scrolls": 0}

        if self._rng.random() >= open_probability:
            return result

        comment_icon = self._first_visible_element(
            [
                'css:section svg[aria-label="Comment"]',
                'xpath://section//*[@aria-label="Comment" and @role="img"]',
            ]
        )
        if comment_icon is None:
            return result

        try:
            self.safe_click_button(comment_icon, settle_s=0.5, pre_click_pause_range_s=(0.4, 1.0))
        except Exception as exc:
            logger.debug("[behavior] browse_comments open failed (%s)", exc)
            return result
        result["opened"] = 1

        # Read through the modal — small scrolls inside the comment dialog.
        read_for_s = self._rng.uniform(*read_seconds_range)
        end = time.monotonic() + read_for_s
        while time.monotonic() < end:
            try:
                self._page.scroll.down(self._rng.randint(120, 320))
            except Exception as exc:
                logger.debug("[behavior] comment-modal scroll failed (%s)", exc)
                break
            result["scrolls"] += 1
            self.idle(0.8, 2.4)

        # Like a few comments. Comment heart icons share the post heart's
        # aria-label, but they live INSIDE the comment list (ul/li).
        target_likes = self._rng.randint(lk_lo, lk_hi)
        for _ in range(target_likes):
            heart = self._first_visible_element(
                [
                    'xpath://ul//li//*[@aria-label="Like" and @role="img"]',
                    'xpath://div[@role="dialog"]//li//*[@aria-label="Like"]',
                ]
            )
            if heart is None:
                break
            try:
                self.safe_click_button(
                    heart, settle_s=0.3, pre_click_pause_range_s=(0.5, 1.1)
                )
                result["comments_liked"] += 1
                self.idle(0.9, 2.1)
            except Exception as exc:
                logger.debug("[behavior] comment like failed (%s); stopping", exc)
                break

        # Close the comments modal — the close button has aria-label="Close"
        # in the dialog corner. If we can't find it, hit Escape as a fallback.
        close_btn = self._first_visible_element(
            [
                'xpath://div[@role="dialog"]//*[@aria-label="Close"]',
                'css:svg[aria-label="Close"]',
            ]
        )
        if close_btn is not None:
            try:
                self.click(close_btn)
            except Exception:
                self._press_escape()
        else:
            self._press_escape()
        self.idle(0.6, 1.4)
        return result

    # internals
    def _first_visible_element(self, selectors: Iterable[str]) -> Any | None:
        """return the first element found."""
        for sel in selectors:
            try:
                ele = self._page.ele(sel, timeout=2)
            except Exception as exc:
                logger.debug("[behavior] selector %r raised (%s)", sel, exc)
                continue
            if ele:
                return ele
        return None

    def _press_escape(self) -> None:
        try:
            self._page.actions.type("")  #  = Escape
        except Exception as exc:
            logger.debug("[behavior] Escape via actions failed (%s)", exc)

    # more internals
    def _coord_of(self, target: Locator) -> Coord:
        """get coordinate of target."""
        if isinstance(target, tuple):
            return int(target[0]), int(target[1])
        try:
            mid = target.rect.midpoint  # type: ignore[union-attr]
        except AttributeError as exc:
            raise TypeError(
                f"target {type(target).__name__} has no `.rect.midpoint`; "
                "pass an element or an (x, y) tuple"
            ) from exc
        return int(mid[0]), int(mid[1])

    def _draw_bezier(self, start: Coord, end: Coord, *, fast: bool) -> None:
        """draws a curve from start to end."""
        sx, sy = start
        ex, ey = end
        if (sx, sy) == (ex, ey):
            return

        dx = ex - sx
        dy = ey - sy
        distance = math.hypot(dx, dy)

        # Place the two control points at 1/3 and 2/3 along the segment,
        # offset perpendicular by a length-proportional random amount.
        perp_x = -dy / distance if distance else 0.0
        perp_y = dx / distance if distance else 0.0
        offset_mag = max(8.0, distance * self._rng.uniform(0.10, 0.28))

        side1 = 1 if self._rng.random() < 0.5 else -1
        side2 = -side1 if self._rng.random() < 0.7 else side1  # mostly S-shape

        cx1 = sx + dx / 3 + perp_x * offset_mag * side1
        cy1 = sy + dy / 3 + perp_y * offset_mag * side1
        cx2 = sx + 2 * dx / 3 + perp_x * offset_mag * side2 * self._rng.uniform(0.6, 1.0)
        cy2 = sy + 2 * dy / 3 + perp_y * offset_mag * side2 * self._rng.uniform(0.6, 1.0)

        steps = self._rng.randint(*self._step_count_range)
        # Scale step count down for very short hops so we don't stutter.
        if distance < 60:
            steps = max(6, steps // 3)

        base_delay = _DEFAULT_FAST_STEP_DELAY_S if fast else _DEFAULT_BASE_STEP_DELAY_S

        prev_xy: Optional[Coord] = None
        for i in range(steps + 1):
            t = i / steps
            bx, by = _cubic_bezier(t, sx, sy, cx1, cy1, cx2, cy2, ex, ey)
            xy: Coord = (int(round(bx)), int(round(by)))
            if xy == prev_xy:
                continue
            prev_xy = xy
            try:
                self._page.actions.move_to(xy, duration=0)
            except Exception as exc:
                # Don't abort the whole flow on a transient mouse issue —
                # log once and continue. The next move_to may still land.
                logger.debug("[behavior] mouse move to %s failed (%s)", xy, exc)
            time.sleep(_step_delay(t, base_delay, self._rng))


# svg to clickable parent
def _walk_up_to_clickable(ele: Any) -> Any | None:
        """find clickable parent."""
    if ele is None:
        return None

    cascade: tuple[tuple[Any, str], ...] = (
        ("@role=button", "role=button ancestor"),
        ("tag:button",   "<button> ancestor"),
        ("tag:a",        "<a> ancestor"),
        (2,              "parent(2) — bounding box"),
        (1,              "parent(1) — immediate parent"),
    )

    for arg, label in cascade:
        try:
            # DrissionPage's .parent() takes either an int (level)
            # OR a locator string (filter). Pass timeout=0 for the
            # filter path so we don't wait for a parent that
            # definitely isn't there.
            if isinstance(arg, int):
                parent = ele.parent(arg)
            else:
                try:
                    parent = ele.parent(arg, timeout=0)
                except TypeError:
                    # Older DP versions don't accept timeout kwarg
                    # on .parent(); fall back to positional call.
                    parent = ele.parent(arg)
        except Exception as exc:
            logger.debug(
                "[behavior] _walk_up_to_clickable: %s raised (%s) — trying next layer",
                label, exc,
            )
            continue

        if parent is not None:
            logger.debug("[behavior] _walk_up_to_clickable matched via %s", label)
            return parent

    logger.debug("[behavior] _walk_up_to_clickable: every layer returned None")
    return None


def _ensure_clickable(ele: Any) -> Any:
    """make sure element can be clicked."""
    if ele is None:
        return None
    try:
        tag = (ele.tag or "").lower()
    except Exception:
        return ele
    # Real HTMLElements that already support JS .click()
    if tag in ("button", "a", "div", "span", "li"):
        return ele
    # SVG (or anything else exotic) → walk up
    walked = _walk_up_to_clickable(ele)
    return walked if walked is not None else ele


# --- Standalone helpers (no HumanBehaviorEngine instance required) ---
# Locator pool for ``dismiss_instagram_modals`` — kept module-level so
# both the engine method and the standalone function read from the
# same source of truth. STRICTLY no class-hash selectors (``_a9--``,
# etc.) — IG rotates those every few months and any locator that
# depends on them rots fast.
_MODAL_DISMISS_SELECTORS: Tuple[str, ...] = (
    # "Not Now" — Turn on Notifications, Save login info?, etc.
    't:button@text()=Not Now',
    't:button@text()=Not now',
    'xpath://button[normalize-space()="Not Now"]',
    'xpath://button[normalize-space()="Not now"]',
    'xpath://*[@role="button" and normalize-space()="Not Now"]',
    'xpath://*[@role="button" and normalize-space()="Not now"]',
    # Generic Cancel inside a dialog.
    't:button@text()=Cancel',
    'xpath://div[@role="dialog"]//button[normalize-space()="Cancel"]',
    'xpath://div[@role="dialog"]//*[@role="button" and normalize-space()="Cancel"]',
    # Close (X) inside a dialog.
    'xpath://div[@role="dialog"]//*[@aria-label="Close"]',
    'xpath://div[@role="dialog"]//svg[@aria-label="Close"]',
    # "OK" — informational acknowledgement modals.
    't:button@text()=OK',
    'xpath://button[normalize-space()="OK"]',
    'xpath://div[@role="button" and normalize-space()="OK"]',
)


def _modal_still_present(page: Any, *, timeout: float = 0.3) -> bool:
    """Return True if a ``<div role="dialog">`` is still on screen."""
    try:
        d = page.ele('xpath://div[@role="dialog"]', timeout=timeout)
        return d is not None
    except Exception:
        return False


def _click_empty_corner(page: Any, *, x: int = 10, y: int = 10) -> bool:
    """click empty space to close popups."""
    try:
        # DP's actions API: move first, then click. Some DP versions
        # accept a (x, y) tuple to move_to; others require keyword args.
        try:
            page.actions.move_to((x, y), duration=0)
        except TypeError:
            page.actions.move_to(x, y)  # alt signature
        page.actions.click()
        time.sleep(0.4)
        return True
    except Exception as exc:
        logger.debug("[behavior] _click_empty_corner(%d,%d) failed (%s)", x, y, exc)
        return False


def _press_escape(page: Any) -> bool:
    """press escape key to close popups."""
    for esc_form, label in (("\x1b", "ASCII \\x1b ESC"), ("\ue00c", "W3C \\ue00c ESC")):
        try:
            page.actions.type(esc_form)
            time.sleep(0.4)
            logger.info("[behavior] dismissed via %s key", label)
            return True
        except Exception as exc:
            logger.debug("[behavior] _press_escape via %s failed (%s)", label, exc)

    # JS keyboard event dispatch — last resort.
    try:
        page.run_js(
            "document.dispatchEvent(new KeyboardEvent('keydown', "
            "{key:'Escape',code:'Escape',keyCode:27,which:27,bubbles:true}));"
            "document.dispatchEvent(new KeyboardEvent('keyup', "
            "{key:'Escape',code:'Escape',keyCode:27,which:27,bubbles:true}));"
        )
        time.sleep(0.4)
        logger.info("[behavior] dismissed via JS-dispatched Escape event")
        return True
    except Exception as exc:
        logger.debug("[behavior] _press_escape via JS dispatch failed (%s)", exc)
        return False


def dismiss_instagram_modals(
    page: Any,
    *,
    per_selector_timeout_s: float = 1.5,
    max_dismissals: int = 3,
    post_click_settle_s: float = 0.6,
    enable_corner_click_fallback: bool = True,
    enable_escape_fallback: bool = True,
) -> int:
    """
    tries to close instagram modals like notifications.
    returns how many modals were closed.
    """
    if max_dismissals < 1 or per_selector_timeout_s <= 0 or post_click_settle_s < 0:
        logger.warning(
            "[behavior] dismiss_instagram_modals: invalid args "
            "(per_selector=%.2f, max=%d, settle=%.2f) — no-op",
            per_selector_timeout_s, max_dismissals, post_click_settle_s,
        )
        return 0

    # --- Layer 1: text-button sweep ---
    dismissed = 0
    for sweep_idx in range(max_dismissals):
        clicked_this_pass = False
        for sel in _MODAL_DISMISS_SELECTORS:
            try:
                ele = page.ele(sel, timeout=per_selector_timeout_s)
            except Exception as exc:
                logger.debug(
                    "[behavior] L1 selector %r lookup raised (%s) — skipping",
                    sel, exc,
                )
                continue
            if not ele:
                continue

            logger.info(
                "[behavior] L1 sweep #%d: clicking %r", sweep_idx + 1, sel,
            )
            try:
                ele.click()
            except Exception as exc:
                logger.debug(
                    "[behavior] L1 click on %r failed (%s) — continuing", sel, exc,
                )
                continue

            dismissed += 1
            clicked_this_pass = True
            try:
                time.sleep(post_click_settle_s)
            except Exception:
                pass
            break  # restart selector sweep from the top

        if not clicked_this_pass:
            break

    if dismissed:
        logger.info("[behavior] L1 cleared %d modal(s) via text buttons", dismissed)

    # --- Layer 2: backdrop click ---
    # Only fires if a dialog is STILL on screen after Layer 1.
    if enable_corner_click_fallback and _modal_still_present(page):
        logger.warning(
            "[behavior] modal still present after L1 — engaging L2 backdrop click"
        )
        if _click_empty_corner(page, x=10, y=10):
            logger.info("[behavior] L2 backdrop click dispatched at (10, 10)")

    # --- Layer 3: ESC key ---
    if enable_escape_fallback and _modal_still_present(page):
        logger.warning(
            "[behavior] modal still present after L2 — engaging L3 ESC key"
        )
        _press_escape(page)

    if not dismissed:
        logger.debug("[behavior] dismiss_instagram_modals: nothing to dismiss via L1")
    return dismissed


# --- Verified coordinate click (module-level, importable) ---

class ClickVerificationError(RuntimeError):
    """Raised when safe_coordinate_click exhausts all retries without
    the verify_locator confirming the click took effect."""


def safe_coordinate_click(
    page: Any,
    locator_string: str,
    timeout: float = 2,
    *,
    verify_locator: Optional[str] = None,
    verify_disappear: bool = False,
    max_retries: int = 3,
    verify_timeout: float = 3.0,
) -> bool:
    """
    clicks on an element by its coordinates safely.
    """
    # Step 1 — locate the target element once.
    try:
        ele = page.ele(locator_string, timeout=timeout)
        if not ele:
            logger.debug(
                "[behavior] safe_coordinate_click: element not found for %r",
                locator_string,
            )
            return False
    except Exception as exc:
        logger.debug(
            "[behavior] safe_coordinate_click: locate failed for %r: %s",
            locator_string, exc,
        )
        return False

    # Step 2 — retry loop.
    for attempt in range(1, max_retries + 1):
        try:
            ele.scroll.to_see(center=True)
            time.sleep(0.5)  # Let React render after scroll

            x, y = ele.rect.midpoint
            page.actions.move_to((x, y)).click()
            logger.debug(
                "[behavior] safe_coordinate_click: click #%d at (%d, %d) for %r",
                attempt, x, y, locator_string,
            )
        except Exception as exc:
            logger.debug(
                "[behavior] safe_coordinate_click: click #%d failed for %r: %s",
                attempt, locator_string, exc,
            )
            if attempt < max_retries:
                time.sleep(1.0)
                # Re-locate in case the element shifted.
                try:
                    ele = page.ele(locator_string, timeout=timeout)
                    if not ele:
                        continue
                except Exception:
                    continue
            continue

        # Step 3 — verify (if requested).
        if verify_locator is None:
            return True  # No verification needed — assume success.

        verified = _check_verification(
            page, verify_locator, verify_disappear, verify_timeout,
        )
        if verified:
            logger.info(
                "[behavior] safe_coordinate_click: verified on attempt #%d "
                "(verify=%r, disappear=%s)",
                attempt, verify_locator, verify_disappear,
            )
            return True

        logger.warning(
            "[behavior] safe_coordinate_click: verification FAILED on attempt "
            "#%d/%d (verify=%r, disappear=%s)",
            attempt, max_retries, verify_locator, verify_disappear,
        )
        if attempt < max_retries:
            time.sleep(1.0)
            # Re-locate in case React re-rendered the target.
            try:
                ele = page.ele(locator_string, timeout=timeout)
                if not ele:
                    logger.debug(
                        "[behavior] safe_coordinate_click: target vanished "
                        "before retry #%d",
                        attempt + 1,
                    )
            except Exception:
                pass

    # All retries exhausted.
    if verify_locator is not None:
        raise ClickVerificationError(
            f"Click verification failed after {max_retries} attempts: "
            f"target={locator_string!r}, verify={verify_locator!r}, "
            f"disappear={verify_disappear}"
        )
    return False


def _check_verification(
    page: Any,
    verify_locator: str,
    verify_disappear: bool,
    verify_timeout: float,
) -> bool:
    """Poll for the verify_locator to appear or disappear."""
    deadline = time.monotonic() + verify_timeout
    while time.monotonic() < deadline:
        try:
            found = page.ele(verify_locator, timeout=0.5)
        except Exception:
            found = None

        if verify_disappear:
            if not found:
                return True  # Element is gone — success.
        else:
            if found:
                return True  # Element appeared — success.

        time.sleep(0.3)

    return False


# --- Pure functions (easy to unit-test, no side effects) ---
def _cubic_bezier(
    t: float,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    x3: float,
    y3: float,
) -> Tuple[float, float]:
    """evaluates a bezier curve."""
    u = 1.0 - t
    bx = u * u * u * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x3
    by = u * u * u * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y3
    return bx, by


def _step_delay(t: float, base_delay: float, rng: random.Random) -> float:
    """calculates delay between mouse movements."""
    # Distance from the midpoint of the trajectory in [0, 0.5].
    edge = abs(t - 0.5)
    # Slowness factor: 1.0 at endpoints, ~0.25 at midpoint.
    slowness = 0.25 + 1.5 * edge * edge
    jitter = rng.uniform(0.85, 1.18)
    return base_delay * slowness * jitter
