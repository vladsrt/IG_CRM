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
from typing import Any, Optional, Protocol, Tuple, Union

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
