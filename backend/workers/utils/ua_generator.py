"""
User-Agent Pool
---------------
Curated, version-pinned pool of recent desktop Chrome User-Agent strings
keyed by host platform. The pool is intentionally small and static —
randomization happens at *selection* time per account, after which the
chosen UA is persisted for the lifetime of that account's session.

Why pin? Because rotating an account's UA between sessions is itself a
strong bot signal — browsers don't change their major version mid-week.
Each ``InstagramAccount`` should pick once and stick with it; this pool
exists only to seed that one-time choice.

Refresh policy
~~~~~~~~~~~~~~
Bump the strings here whenever stable Chrome ships a new major version
that's been on at least 30% of the install base for two weeks. The
tighter UAs in this pool track the channel `chrome://settings/help`
reports for the corresponding desktop OS.
"""

from __future__ import annotations

import enum
import random
from typing import Dict, List, Optional


class Platform(str, enum.Enum):
    """Operating system the spoofed Chromium build advertises."""

    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"


# ── The pool ────────────────────────────────────────────────────────────
# Strings track stable Chrome on each desktop OS. Chromium-only — we do
# NOT include Edge, Opera, Brave, or mobile builds, because the surface
# fingerprint that ships alongside (UA-CH, navigator.platform, GPU
# vendor strings) won't match and IG's bot heuristics will notice.
USER_AGENT_POOL: Dict[Platform, List[str]] = {
    Platform.WINDOWS: [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36",
    ],
    Platform.MACOS: [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36",
    ],
    Platform.LINUX: [
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/132.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0.0.0 Safari/537.36",
    ],
}


def get_random_user_agent(
    platform: Platform | str,
    *,
    rng: Optional[random.Random] = None,
) -> str:
    """Return one random Chrome UA from the pool for ``platform``.

    Args:
        platform: The platform to pick a UA for. Accepts the
            :class:`Platform` enum or its string value
            (``"windows"`` / ``"macos"`` / ``"linux"``).
        rng: Optional ``random.Random`` instance. Pass a seeded one
            from tests; production callers leave this as ``None`` and
            get the module's shared RNG.

    Raises:
        ValueError: if ``platform`` is not a valid platform name or the
            pool for that platform is unexpectedly empty (refresh bug).
    """
    if isinstance(platform, str):
        try:
            platform = Platform(platform.lower())
        except ValueError as exc:
            raise ValueError(
                f"unknown platform {platform!r} — expected one of "
                f"{[p.value for p in Platform]}"
            ) from exc

    pool = USER_AGENT_POOL.get(platform)
    if not pool:
        raise ValueError(
            f"UA pool for platform {platform.value!r} is empty — "
            "the static pool needs a refresh"
        )

    chooser = rng if rng is not None else random
    return chooser.choice(pool)


__all__ = ["Platform", "USER_AGENT_POOL", "get_random_user_agent"]
