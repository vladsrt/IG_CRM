"""Parallel-agent slot accounting.

An "agent" is one concurrent browser/task slot. The number a user gets comes
from their subscription: an explicit admin override (`agents_limit`) wins,
otherwise it defaults from the billing tier.
"""

from __future__ import annotations

from app.core.config import settings
from app.models.billing import Subscription


def tier_default_agents(tier: str | None) -> int:
    t = (tier or "free").lower()
    if t == "enterprise":
        return settings.AGENTS_ENTERPRISE
    if t == "pro":
        return settings.AGENTS_PRO
    return settings.AGENTS_FREE


def agents_for_subscription(sub: Subscription | None) -> int:
    if sub is None:
        return settings.AGENTS_FREE
    if sub.agents_limit is not None:
        return max(0, sub.agents_limit)
    return tier_default_agents(sub.tier)
