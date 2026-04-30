"""
Observability for browser-side execution
----------------------------------------
Two cooperating background watchers attach to the live ``ChromiumPage``
while ``TaskExecutor`` is running commands:

1. **Network interceptor** (Task 6.1)
   Listens for IG GraphQL / private-API JSON responses. Pulls
   ``follower_count``, ``reach``, and reel ``view_count`` / ``play_count``
   out of the body and queues them as ``MetricSample`` records. The
   executor flushes the queue to ``account_metrics`` after the run.

2. **Checkpoint detector** (Task 6.2)
   Polls ``page.url`` on a short cadence. If IG redirects to
   ``/challenge/`` or ``/accounts/suspended/`` the watcher trips a flag;
   the executor calls :py:meth:`ObservabilityMonitor.check_checkpoint`
   between commands and raises :class:`CheckpointException` so the Celery
   wrapper can mark the Task FAILED and the account ``checkpoint_required``.

Both watchers are daemon threads — they cannot keep the worker process
alive on their own and they tear down idempotently on ``stop()``.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional

logger = logging.getLogger(__name__)


# ── Public errors ───────────────────────────────────────────────────────
class CheckpointException(Exception):
    """Raised when IG redirects the session to a challenge / suspension page.

    The catching layer (``run_instagram_task``) is expected to:
      * mark the Task ``FAILED``
      * mark the InstagramAccount ``status='checkpoint_required'``
      * persist the offending URL to ``Task.error_log``
    """

    def __init__(self, url: str) -> None:
        super().__init__(f"Account hit a checkpoint at {url}")
        self.url = url


# ── Public data records ─────────────────────────────────────────────────
@dataclass(slots=True)
class MetricSample:
    """One in-memory metric data-point produced by the network listener."""

    metric_type: str  # 'followers' | 'reach' | 'reel_views'
    value: int
    reel_pk: Optional[str] = None
    raw_payload: Optional[dict[str, Any]] = None


# ── Tunables ────────────────────────────────────────────────────────────
_DEFAULT_URL_POLL_INTERVAL_S: float = 1.0
_DEFAULT_LISTEN_TIMEOUT_S: float = 2.0
_CHECKPOINT_URL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"/challenge/", re.IGNORECASE),
    re.compile(r"/accounts/suspended", re.IGNORECASE),
)
_INTERCEPT_URL_TARGETS: tuple[str, ...] = (
    "i.instagram.com/api/",
    "instagram.com/api/v1/",
    "instagram.com/graphql",
    "instagram.com/api/graphql",
)
_RAW_PAYLOAD_TRIM_KEYS: tuple[str, ...] = (
    "follower_count",
    "following_count",
    "media_count",
    "reach",
    "reach_count",
    "view_count",
    "play_count",
    "ig_play_count",
    "id",
    "pk",
    "code",
    "media_type",
)


# ── Monitor ─────────────────────────────────────────────────────────────
class ObservabilityMonitor:
    """Owns the URL-poller and network-listener threads for one task run."""

    def __init__(
        self,
        page: Any,
        account_id: uuid.UUID,
        *,
        url_poll_interval_s: float = _DEFAULT_URL_POLL_INTERVAL_S,
        intercept_targets: Iterable[str] = _INTERCEPT_URL_TARGETS,
    ) -> None:
        self._page = page
        self._account_id = account_id
        self._url_poll_interval_s = url_poll_interval_s
        self._intercept_targets = list(intercept_targets)

        self._stop_event = threading.Event()
        self._url_thread: Optional[threading.Thread] = None
        self._listen_thread: Optional[threading.Thread] = None

        self._checkpoint_url: Optional[str] = None
        self._checkpoint_lock = threading.Lock()

        self._samples: List[MetricSample] = []
        self._samples_lock = threading.Lock()

        self._started = False
        self._stopped = False

    # ── Lifecycle ──────────────────────────────────────────────────────
    def start(self) -> None:
        """Begin background watchers. Idempotent."""
        if self._started:
            return
        self._started = True

        self._url_thread = threading.Thread(
            target=self._url_loop,
            name=f"obs-url-{self._account_id}",
            daemon=True,
        )
        self._url_thread.start()

        self._listen_thread = threading.Thread(
            target=self._listen_loop,
            name=f"obs-net-{self._account_id}",
            daemon=True,
        )
        self._listen_thread.start()
        logger.info(
            "[observability] started for account_id=%s (url-poll=%.1fs, targets=%d)",
            self._account_id,
            self._url_poll_interval_s,
            len(self._intercept_targets),
        )

    def stop(self, *, join_timeout_s: float = 3.0) -> None:
        """Signal both threads to exit and wait briefly. Idempotent."""
        if self._stopped:
            return
        self._stopped = True
        self._stop_event.set()

        try:
            self._page.listen.stop()
        except Exception as exc:
            logger.debug("[observability] page.listen.stop raised (%s); ignoring", exc)

        for thread in (self._url_thread, self._listen_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=join_timeout_s)
        logger.info(
            "[observability] stopped for account_id=%s; collected %d sample(s)",
            self._account_id,
            len(self._samples),
        )

    # ── Public probes (called from TaskExecutor) ───────────────────────
    def check_checkpoint(self) -> None:
        """Raise :class:`CheckpointException` if the URL poller flagged one."""
        with self._checkpoint_lock:
            url = self._checkpoint_url
        if url is not None:
            raise CheckpointException(url)

    def drain_samples(self) -> List[MetricSample]:
        """Atomically take all buffered samples and reset the buffer."""
        with self._samples_lock:
            out = list(self._samples)
            self._samples.clear()
        return out

    @property
    def account_id(self) -> uuid.UUID:
        return self._account_id

    # ── URL-change watcher (Task 6.2) ──────────────────────────────────
    def _url_loop(self) -> None:
        last_seen: str = ""
        while not self._stop_event.is_set():
            try:
                current = str(self._page.url or "")
            except Exception as exc:
                logger.debug("[observability] page.url read failed (%s)", exc)
                self._stop_event.wait(self._url_poll_interval_s)
                continue

            if current and current != last_seen:
                last_seen = current
                if any(p.search(current) for p in _CHECKPOINT_URL_PATTERNS):
                    with self._checkpoint_lock:
                        self._checkpoint_url = current
                    logger.warning(
                        "[observability] checkpoint URL detected: %s", current
                    )
                    # Keep the loop alive so subsequent checks still raise — the
                    # executor will catch the exception on its next tick.
            self._stop_event.wait(self._url_poll_interval_s)

    # ── Network listener (Task 6.1) ────────────────────────────────────
    def _listen_loop(self) -> None:
        try:
            self._page.listen.start(self._intercept_targets)
        except Exception:
            logger.exception("[observability] page.listen.start failed; aborting listener")
            return

        while not self._stop_event.is_set():
            try:
                packet = self._page.listen.wait(
                    count=1, timeout=_DEFAULT_LISTEN_TIMEOUT_S
                )
            except Exception as exc:
                logger.debug("[observability] page.listen.wait raised (%s)", exc)
                continue
            if packet is None or packet is False:
                continue
            try:
                self._ingest_packet(packet)
            except Exception:
                logger.exception("[observability] packet handler failed")

    def _ingest_packet(self, packet: Any) -> None:
        url = getattr(packet, "url", "") or ""
        response = getattr(packet, "response", None)
        if response is None:
            return
        body = getattr(response, "body", None)
        if not isinstance(body, (dict, list)):
            return

        for sample in _extract_samples(url, body):
            with self._samples_lock:
                self._samples.append(sample)
            logger.debug(
                "[observability] captured %s=%s reel_pk=%s",
                sample.metric_type, sample.value, sample.reel_pk,
            )


# ── Pure extraction helpers (unit-testable, no I/O) ─────────────────────
def _extract_samples(url: str, body: Any) -> List[MetricSample]:
    """Walk a JSON response and yield ``MetricSample``s for known shapes."""
    out: List[MetricSample] = []
    _walk(body, url, out)
    return out


def _walk(node: Any, url: str, out: List[MetricSample], _depth: int = 0) -> None:
    """Recursively walk a JSON document looking for known metric keys.

    Bounded recursion (`_depth`) keeps a hostile / cyclic payload from
    blowing the stack. IG bodies are well-behaved, but the listener runs
    on whatever the wire delivers.
    """
    if _depth > 12:
        return

    if isinstance(node, dict):
        # Account-scoped: follower_count
        follower_count = node.get("follower_count")
        if isinstance(follower_count, int):
            out.append(MetricSample(
                metric_type="followers",
                value=int(follower_count),
                raw_payload=_trim(node),
            ))

        # Account-scoped: reach (insights endpoint variants)
        for reach_key in ("reach", "reach_count"):
            reach = node.get(reach_key)
            if isinstance(reach, int):
                out.append(MetricSample(
                    metric_type="reach",
                    value=int(reach),
                    raw_payload=_trim(node),
                ))
                break

        # Reel-scoped: play_count / view_count
        media_type = node.get("media_type")
        product_type = node.get("product_type")
        is_reel = (
            media_type == 2  # video
            or product_type in {"clips", "reel", "reels"}
            or "clips" in url
            or "reels" in url
        )
        play_count = node.get("play_count") or node.get("ig_play_count") or node.get("view_count")
        if is_reel and isinstance(play_count, int):
            reel_pk = _coerce_str(node.get("pk") or node.get("id") or node.get("code"))
            out.append(MetricSample(
                metric_type="reel_views",
                value=int(play_count),
                reel_pk=reel_pk,
                raw_payload=_trim(node),
            ))

        for v in node.values():
            _walk(v, url, out, _depth + 1)

    elif isinstance(node, list):
        for item in node:
            _walk(item, url, out, _depth + 1)


def _trim(node: dict[str, Any]) -> dict[str, Any]:
    """Keep only diagnostically useful keys from a node before persisting."""
    return {k: node[k] for k in _RAW_PAYLOAD_TRIM_KEYS if k in node}


def _coerce_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return None
