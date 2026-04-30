"""Trust Score — pre-flight safety gate for fan-out dispatch (Epic 8.1).

For every account about to receive a Task we compute a 0-100 score by
combining three signals:

    +-----------+---------+-------------------------------------------------+
    | Signal    | Weight  | Notes                                           |
    +===========+=========+=================================================+
    | Proxy     |   60    | Authenticated CONNECT to a low-cost target;     |
    |           |         | hard-fail (score=0) on connection error so a    |
    |           |         | dead proxy never dispatches anywhere.           |
    +-----------+---------+-------------------------------------------------+
    | UA        |   25    | Non-empty, plausible-shape user agent           |
    +-----------+---------+-------------------------------------------------+
    | Hygiene   |   15    | Penalize accounts already flagged with a known  |
    |           |         | risk tag (possible_shadowban, checkpoint_*) or  |
    |           |         | a non-OK status                                 |
    +-----------+---------+-------------------------------------------------+

The score is **mocked** in the sense that the proxy-ping uses a single
HEAD request to a static endpoint rather than a multi-region latency
probe — but the function shape, the weighting, and the structured result
(:class:`TrustReport`) are production-real.
"""

from __future__ import annotations

import logging
import re
import socket
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from app.models.account import InstagramAccount
from app.models.proxy import Proxy

logger = logging.getLogger(__name__)


# ── Tunables ────────────────────────────────────────────────────────────
PROXY_PROBE_URL: str = "https://www.instagram.com/robots.txt"
PROXY_PROBE_TIMEOUT_S: float = 8.0

PROXY_WEIGHT: int = 60
USER_AGENT_WEIGHT: int = 25
HYGIENE_WEIGHT: int = 15

DEFAULT_MIN_TRUST_SCORE: int = 50
"""Below this, the fan-out endpoint refuses to dispatch the task."""

_RISK_TAGS: frozenset[str] = frozenset(
    {"possible_shadowban", "checkpoint", "banned", "frozen"}
)
_BAD_STATUSES: frozenset[str] = frozenset(
    {"checkpoint_required", "banned", "invalid", "possible_shadowban"}
)
_USER_AGENT_PATTERN = re.compile(
    r"^Mozilla/5\.0\s*\([^)]+\).*\b(Chrome|Safari|Firefox|Edg)/[\d.]+",
    re.IGNORECASE,
)


# ── Public records ──────────────────────────────────────────────────────
@dataclass(slots=True)
class TrustReport:
    """Structured outcome of a single trust evaluation."""

    score: int
    proxy_ok: bool
    proxy_latency_ms: Optional[int]
    user_agent_ok: bool
    hygiene_ok: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Convenience: True iff score is at or above the configured threshold."""
        return self.score >= DEFAULT_MIN_TRUST_SCORE

    def to_skip_reason(self) -> str:
        """One-line summary suitable for ``FanOutSkippedAccount.reason``."""
        head = f"low_trust_score (score={self.score}/100)"
        if self.reasons:
            return f"{head}: {'; '.join(self.reasons)}"
        return head


# ── Public API ──────────────────────────────────────────────────────────
def evaluate_trust(
    account: InstagramAccount,
    *,
    user_agent: str,
    proxy: Optional[Proxy] = None,
    skip_proxy_probe: bool = False,
) -> TrustReport:
    """Return a :class:`TrustReport` for ``account``.

    ``proxy`` defaults to ``account.proxy`` when omitted. Pass
    ``skip_proxy_probe=True`` in unit tests to avoid the network round-trip.
    """
    proxy = proxy if proxy is not None else account.proxy

    proxy_ok, proxy_latency_ms, proxy_reason = _evaluate_proxy(
        proxy, skip_probe=skip_proxy_probe
    )
    ua_ok, ua_reason = _evaluate_user_agent(user_agent)
    hygiene_ok, hygiene_reason = _evaluate_hygiene(account)

    score = (
        (PROXY_WEIGHT if proxy_ok else 0)
        + (USER_AGENT_WEIGHT if ua_ok else 0)
        + (HYGIENE_WEIGHT if hygiene_ok else 0)
    )

    reasons: list[str] = []
    for ok, reason in (
        (proxy_ok, proxy_reason),
        (ua_ok, ua_reason),
        (hygiene_ok, hygiene_reason),
    ):
        if not ok and reason:
            reasons.append(reason)

    return TrustReport(
        score=score,
        proxy_ok=proxy_ok,
        proxy_latency_ms=proxy_latency_ms,
        user_agent_ok=ua_ok,
        hygiene_ok=hygiene_ok,
        reasons=reasons,
    )


# ── Probes ──────────────────────────────────────────────────────────────
def _evaluate_proxy(
    proxy: Optional[Proxy], *, skip_probe: bool
) -> tuple[bool, Optional[int], Optional[str]]:
    """Verify the proxy answers a HEAD against a low-cost endpoint."""
    if proxy is None:
        return False, None, "no proxy attached"

    proxy_url = (
        f"http://{proxy.username}:{proxy.password}@{proxy.host}:{proxy.port}"
    )
    if skip_probe:
        return True, None, None

    parsed = urlparse(PROXY_PROBE_URL)
    host_port = (parsed.hostname or "", parsed.port or 443)

    # Step 1 — DNS-level liveness on the proxy host itself. Catches a dead
    # provider before we waste the HTTPS round-trip budget.
    try:
        socket.getaddrinfo(proxy.host, proxy.port)
    except socket.gaierror as exc:
        return False, None, f"proxy DNS failed: {exc}"

    # Step 2 — actual HEAD via the proxy. Uses urllib so we keep zero
    # third-party deps in the trust path.
    handler = ProxyHandler({"http": proxy_url, "https": proxy_url})
    opener = build_opener(handler)
    req = Request(PROXY_PROBE_URL, method="HEAD")
    try:
        import time as _time
        t0 = _time.monotonic()
        with opener.open(req, timeout=PROXY_PROBE_TIMEOUT_S) as resp:
            status = resp.status
        latency_ms = int((_time.monotonic() - t0) * 1000)
    except Exception as exc:
        return False, None, f"proxy probe failed: {type(exc).__name__}: {exc}"

    if status >= 500:
        return False, latency_ms, f"proxy upstream {status}"
    return True, latency_ms, None


def _evaluate_user_agent(user_agent: str) -> tuple[bool, Optional[str]]:
    if not user_agent or not isinstance(user_agent, str):
        return False, "user_agent missing"
    if len(user_agent) < 32 or len(user_agent) > 512:
        return False, f"user_agent length {len(user_agent)} out of bounds"
    if not _USER_AGENT_PATTERN.search(user_agent):
        return False, "user_agent does not match a recognized desktop browser shape"
    return True, None


def _evaluate_hygiene(account: InstagramAccount) -> tuple[bool, Optional[str]]:
    """Penalize accounts already flagged by upstream signals (Epic 6, etc.)."""
    if account.status and account.status.lower() in _BAD_STATUSES:
        return False, f"account.status={account.status!r}"
    risky_tags = [t for t in (account.tags or []) if t in _RISK_TAGS]
    if risky_tags:
        return False, f"account carries risk tags {risky_tags}"
    return True, None
