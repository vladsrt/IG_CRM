"""Trust score, the pre-flight gate before fan-out dispatch.

For every account that is about to get a Task we build a 0-100 score from
three signals:

    proxy    (60): authed CONNECT to a cheap target. Hard-fail (score=0)
                   on a connection error so a dead proxy never dispatches.
    ua       (25): user agent must be set and look like a real browser.
    hygiene  (15): drop the score if the account already has a risk tag
                   (possible_shadowban, checkpoint_*) or a non-OK status.

The score is a bit "mocked": the proxy ping is a single HEAD to a static
endpoint, not a multi-region latency probe. But the function shape, the
weights and the TrustReport result are the real thing for production.
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


# knobs
PROXY_PROBE_URL: str = "https://www.instagram.com/robots.txt"
PROXY_PROBE_TIMEOUT_S: float = 8.0

PROXY_WEIGHT: int = 60
USER_AGENT_WEIGHT: int = 25
HYGIENE_WEIGHT: int = 15

DEFAULT_MIN_TRUST_SCORE: int = 50
"""Below this the fan-out route will not dispatch the task."""

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


# public records
@dataclass(slots=True)
class TrustReport:
    """One trust check result, structured."""

    score: int
    proxy_ok: bool
    proxy_latency_ms: Optional[int]
    user_agent_ok: bool
    hygiene_ok: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """True if the score is at or above the threshold."""
        return self.score >= DEFAULT_MIN_TRUST_SCORE

    def to_skip_reason(self) -> str:
        """One-line summary that fits FanOutSkippedAccount.reason."""
        head = f"low_trust_score (score={self.score}/100)"
        if self.reasons:
            return f"{head}: {'; '.join(self.reasons)}"
        return head


# public api
def evaluate_trust(
    account: InstagramAccount,
    *,
    user_agent: str,
    proxy: Optional[Proxy] = None,
    skip_proxy_probe: bool = False,
) -> TrustReport:
    """Build a TrustReport for the account.

    If `proxy` is not given, falls back to account.proxy. Pass
    skip_proxy_probe=True in unit tests so we do not hit the network.
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


# probes
def _evaluate_proxy(
    proxy: Optional[Proxy], *, skip_probe: bool
) -> tuple[bool, Optional[int], Optional[str]]:
    """Check the proxy can answer a HEAD on a cheap endpoint."""
    if proxy is None:
        return False, None, "no proxy attached"

    proxy_url = (
        f"http://{proxy.username}:{proxy.password}@{proxy.host}:{proxy.port}"
    )
    if skip_probe:
        return True, None, None

    parsed = urlparse(PROXY_PROBE_URL)
    host_port = (parsed.hostname or "", parsed.port or 443)

    # step 1: DNS check on the proxy host. catches a dead provider before
    # we spend the https round-trip budget.
    try:
        socket.getaddrinfo(proxy.host, proxy.port)
    except socket.gaierror as exc:
        return False, None, f"proxy DNS failed: {exc}"

    # step 2: HEAD through the proxy. uses urllib so the trust path has
    # zero third-party deps.
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
    """Drop the score if upstream signals already flagged the account."""
    if account.status and account.status.lower() in _BAD_STATUSES:
        return False, f"account.status={account.status!r}"
    risky_tags = [t for t in (account.tags or []) if t in _RISK_TAGS]
    if risky_tags:
        return False, f"account carries risk tags {risky_tags}"
    return True, None
