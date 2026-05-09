"""
Human Behavior Engine
---------------------
Centralized "humanizer" used by every action handler that drives a real
browser. Replaces raw DrissionPage interactions (``ele.click()``,
``ele.input(text)``, instantaneous coordinate moves) with mouse
trajectories, keystroke timing, reading pauses, and scroll noise designed
to defeat heuristic anti-bot signals from Meta's risk engine.

Public API
~~~~~~~~~~
``HumanBehaviorEngine(page)``
    .move_to(target)             — bezier-traced cursor move
    .click(target)               — move_to + click (with optional overshoot)
    .type_into(target, text)     — variable-cadence keystrokes
    .read_pause(content_length)  — sleep weighted by amount of content
    .micro_scroll()              — 1-3 small wheel deltas in random direction
    .idle(min_s, max_s)          — short uniform jitter

The engine NEVER calls ``page.actions.move_to(target, duration=0)`` or
sets cursor coordinates directly — every move is interpolated through a
cubic Bezier curve sampled into discrete steps with ease-in / ease-out
timing. Direct teleportation would produce a single ``mousemove`` event
with no path history, which Instagram's client-side telemetry treats as
a strong bot signal.

Typing reference
~~~~~~~~~~~~~~~~
This module is fully type-annotated and intended to be mypy-strict-clean.
``Locator`` is the union of types DrissionPage's ``Actions.move_to`` will
accept (an element handle or an absolute ``(x, y)`` coordinate tuple).
"""

from __future__ import annotations

import logging
import math
import random
import time
from typing import Any, Dict, Iterable, Optional, Protocol, Tuple, Union

logger = logging.getLogger(__name__)


# ── Type aliases ────────────────────────────────────────────────────────
Coord = Tuple[int, int]
"""An absolute ``(x, y)`` cursor coordinate in viewport pixels."""

Locator = Union["_HasRect", Coord]
"""What the engine accepts as a target — a DrissionPage element OR a coord."""


class _HasRect(Protocol):
    """Structural protocol for DrissionPage element handles.

    Defined as a Protocol (not a hard import) so this module remains
    importable in environments where ``DrissionPage`` is not installed
    (CI, unit tests, type-checking only).
    """

    rect: Any  # DrissionPage exposes .rect.midpoint as a (x, y) tuple

    def click(self) -> Any: ...
    def input(self, text: str, clear: bool = ...) -> Any: ...


# ── Tunables ────────────────────────────────────────────────────────────
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
    """Inject human-like timing into a DrissionPage ``ChromiumPage`` session.

    One engine per browser session is the expected pattern — it is created
    inside an action handler and passed nowhere. Engines hold *no* shared
    state across handlers beyond the current cursor position.
    """

    def __init__(
        self,
        page: Any,
        *,
        overshoot_probability: float = _DEFAULT_OVERSHOOT_PROBABILITY,
        step_count_range: Tuple[int, int] = _DEFAULT_STEP_COUNT_RANGE,
        rng_seed: Optional[int] = None,
    ) -> None:
        """Initialize the engine.

        Args:
            page: The DrissionPage ``ChromiumPage`` instance owned by an
                ``InstagramBrowser``.
            overshoot_probability: Probability that ``click()`` will
                deliberately overshoot the target by a few pixels and then
                correct. Must be in ``[0.0, 1.0]``.
            step_count_range: Inclusive ``(min, max)`` number of points
                sampled along each Bezier curve. Higher = smoother but
                slower trajectories.
            rng_seed: Optional seed for deterministic behavior in tests.
                Production callers pass ``None``.
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

    # ── Public API ─────────────────────────────────────────────────────
    def move_to(self, target: Locator) -> Coord:
        """Move the cursor to ``target`` along a sampled Bezier curve.

        Returns the final cursor coordinate. Never teleports. Optionally
        overshoots and corrects. Safe to call when the cursor is already
        near the target — the curve degenerates to a short, low-step path.
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
            self.idle(0.06, 0.14)  # micro-pause before correction
            self._draw_bezier((ox, oy), target_xy, fast=True)
        else:
            self._draw_bezier(self._cursor, target_xy, fast=False)

        self._cursor = target_xy
        return target_xy

    def click(self, target: Locator) -> None:
        """Move to ``target`` then click it via DrissionPage's ``Actions``.

        We deliberately use ``page.actions.click()`` (which dispatches a
        synthetic ``mousedown``/``mouseup`` pair AT the current cursor
        position) rather than ``ele.click()`` (which jumps to the
        element's center with no path). This preserves the trajectory we
        just drew.
        """
        self.move_to(target)
        self.idle(0.05, 0.18)
        try:
            self._page.actions.click()
        except Exception as exc:
            logger.debug("[behavior] actions.click failed (%s); falling back to ele.click", exc)
            if hasattr(target, "click"):
                target.click()  # type: ignore[union-attr]
            else:
                raise

    def safe_click_button(
        self,
        target: _HasRect,
        *,
        settle_s: float = 1.0,
        pre_click_pause_range_s: Tuple[float, float] = (0.4, 0.9),
    ) -> None:
        """Click a button after guaranteeing it is visible in the viewport.

        Off-screen ``Submit`` / ``Save`` / ``Share`` buttons are the #1 cause
        of silent click failures in DrissionPage — the synthetic mousedown
        lands on whatever element happens to be under the cursor at that
        coordinate, not the intended button. This helper:

        1. Calls ``element.scroll.to_see(center=True)`` to bring the button
           into the viewport center.
        2. Waits ``settle_s`` seconds for the layout to stabilize (IG often
           reflows after a scroll, especially with sticky headers).
        3. Adds a short pre-click hesitation — humans don't click a button
           the millisecond it appears.
        4. Routes the click through :meth:`click` so the cursor still
           travels along a Bezier path.

        Always prefer this over plain ``.click()`` for any commit-style
        button (Submit, Save, Share, Confirm, Post).
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
        backspace_passes: int = 1,  # kept for API compat; ignored by the JS path
    ) -> bool:
        """Hard-clear an input/textarea/contenteditable via JS injection.

        React-controlled forms (Instagram's bio editor, the upload
        caption, every place we type) ignore hardware ``Backspace``
        keys whenever the React state for the node holds a non-empty
        value — the hardware keystroke clears the DOM, then React
        re-renders the old value back in on the next tick. The only
        reliable wipe is to write directly to the node's ``value`` AND
        ``textContent``, then dispatch a synthetic ``input`` event
        (the one React listens for) so its internal state catches up
        to the DOM.

        Strategy:

        1. Focus the target so the caret is in the right place when
           the caller later types into it.
        2. Run a JS payload on the node that:
             * sets ``.value`` (input/textarea path),
             * sets ``.textContent`` and ``.innerText`` (contenteditable),
             * dispatches a bubbling ``input`` event so React reconciles.
        3. Read the field back. If it's still non-empty (rare, but
           possible if React owns the value entirely), fall back to a
           keystroke-based ``Ctrl+A`` + ``Backspace`` retry.

        Returns:
            ``True`` if the field is empty when we return, ``False`` if
            we couldn't fully wipe it. Callers should check this and
            decide whether to abort the action — typing into a
            non-empty field will *append*, which is the bug we're
            fixing here.
        """
        if backspace_passes < 1:
            raise ValueError(f"backspace_passes must be >= 1, got {backspace_passes}")

        if focus_first:
            self.click(target)
            self.idle(0.18, 0.45)

        # ── Step 1 — JS wipe on the element handle ─────────────────────
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

        # ── Step 2 — verify empty ──────────────────────────────────────
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

        # ── Step 3 — keystroke fallback if anything remains ────────────
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
        """Type ``text`` into ``target`` one character at a time.

        Each keystroke is followed by a randomized inter-key delay. With
        small probability a longer "thinking" pause is inserted to mimic
        a human stopping mid-sentence.

        ``target`` must be a DrissionPage element (not a coord).
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
        """Sleep as if the operator were reading.

        When ``content_length`` is provided, the pause is roughly
        ``content_length / READING_CHARS_PER_SECOND`` with ±30% jitter
        (clamped to ``[_READING_MIN_S, _READING_MAX_S]``). When ``None``,
        a short uniform pause is used.
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
        """Emit 1-3 small wheel deltas in a randomized direction.

        Real users skim — they micro-scroll between actions. Pure-bot
        sessions almost never do. The default mix is mostly downward with
        an occasional upward correction.
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
        """Short uniform sleep — the universal "human is breathing" beat."""
        if min_s < 0 or max_s < min_s:
            raise ValueError(f"Invalid idle range: ({min_s}, {max_s})")
        time.sleep(self._rng.uniform(min_s, max_s))

    # ── JS-driven smooth scroll (preferred over DrissionPage's hard scrolls) ──
    def smooth_scroll(
        self,
        min_y: int = 300,
        max_y: int = 800,
        *,
        upward_probability: float = 0.15,
        post_scroll_pause_range_s: Tuple[float, float] = (0.8, 2.6),
    ) -> int:
        """Smoothly scroll the page by a randomized amount via injected JS.

        DrissionPage's ``page.scroll.down(N)`` issues an instant
        ``scrollTo``-style jump that produces a single, choppy scroll
        event with no intermediate frames. Real browsers, when a user
        rolls the wheel, emit dozens of incremental scroll events as the
        viewport eases to its new position. We replicate that by calling
        ``window.scrollBy({top: y, behavior: 'smooth'})`` — the browser
        handles the easing animation natively.

        Args:
            min_y: Lower bound on scroll distance (pixels). Must be > 0.
            max_y: Upper bound on scroll distance (pixels).
            upward_probability: Chance the scroll goes UP instead of down,
                simulating a user who saw something interesting and went
                back to look at it. Defaults to 15%.
            post_scroll_pause_range_s: After-scroll pause range. The pause
                always happens — humans don't fire wheel events
                back-to-back without at least a beat to look at the new
                content.

        Returns:
            Signed pixel delta actually requested (negative = upward).
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
        """Click ``target`` with humanized timing, swallowing locator errors.

        Behaviour:

        1. If ``hover_first`` and the target has a ``.rect`` (i.e. it's a
           DrissionPage element, not a coord), move the cursor to it
           along a Bezier path — that's the "hover".
        2. Sleep ``pre_click_pause_range_s`` (default 0.2-0.7s) — a real
           user doesn't click the instant the cursor lands.
        3. Issue the click.

        EVERYTHING is wrapped in try/except. If ``target`` is ``None``, a
        stale element, or the click itself raises, this method returns
        ``False`` instead of propagating — exactly what the warmup loop
        wants, since "the comment icon wasn't visible this iteration"
        should never crash a 15-minute session.

        Returns:
            True iff the click was issued without raising.
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

    # ── Deep humanization helpers (Warmup 2.0) ─────────────────────────
    def deep_scroll_session(
        self,
        *,
        duration_s: float,
        upward_correction_probability: float = 0.18,
        flick_probability: float = 0.12,
    ) -> Dict[str, int]:
        """Scroll the feed for ~``duration_s`` seconds with rich variance.

        Behaviour mix per tick (chosen randomly each iteration):

        * **Slow read** — small downward delta + a long reading pause
          weighted by an imagined post length.
        * **Skim** — medium downward delta + short pause.
        * **Flick** — large fast scroll burst (3-6 stacked deltas) with
          almost no pause, simulating a bored thumb-flick.
        * **Upward correction** — occasional scroll-back, like a user
          who saw something interesting and went back to look at it.

        Returns a small counter dict for logging in action results.
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
        """With ``like_probability``, like the post currently centered in view.

        Looks for an unliked Like button (``aria-label="Like"``) on a
        post visible in the viewport and clicks it humanly. If the
        button is already in the "Unlike" state we skip — accidentally
        un-liking a post on the visible feed is a real-user-impact bug.

        Returns True iff a like was actually clicked.
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
        """Maybe open a post's comments, scroll through them, like 0-2.

        With ``open_probability`` we click the comments icon on the
        post currently in the viewport, scroll through the list as if
        reading replies, and like a random subset of comments. Returns
        a counter dict suitable for the warmup result log.
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

    # ── Internals for the helpers above ────────────────────────────────
    def _first_visible_element(self, selectors: Iterable[str]) -> Any | None:
        """Return the first selector that resolves to an element, or None."""
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

    # ── Internals ──────────────────────────────────────────────────────
    def _coord_of(self, target: Locator) -> Coord:
        """Resolve a target into an absolute ``(x, y)`` coordinate."""
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
        """Sample a cubic Bezier from ``start`` to ``end`` and walk it.

        Control points are placed perpendicular to the start→end vector at
        a fraction of its length, on opposite sides. This produces the
        gentle S-curves and one-sided arcs that real wrist motion
        generates, rather than a straight line.
        """
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


# ── Pure functions (easy to unit-test, no side effects) ─────────────────
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
    """Evaluate a cubic Bezier at parameter ``t in [0, 1]``."""
    u = 1.0 - t
    bx = u * u * u * x0 + 3 * u * u * t * x1 + 3 * u * t * t * x2 + t * t * t * x3
    by = u * u * u * y0 + 3 * u * u * t * y1 + 3 * u * t * t * y2 + t * t * t * y3
    return bx, by


def _step_delay(t: float, base_delay: float, rng: random.Random) -> float:
    """Per-step sleep with ease-in / ease-out shape.

    Real wrist motion accelerates from rest, peaks mid-flight, then
    decelerates onto the target. We approximate this with an inverted
    cubic ease — slowest at the endpoints, fastest near ``t == 0.5``.
    """
    # Distance from the midpoint of the trajectory in [0, 0.5].
    edge = abs(t - 0.5)
    # Slowness factor: 1.0 at endpoints, ~0.25 at midpoint.
    slowness = 0.25 + 1.5 * edge * edge
    jitter = rng.uniform(0.85, 1.18)
    return base_delay * slowness * jitter
