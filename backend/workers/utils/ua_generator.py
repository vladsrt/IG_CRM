"""User-Agent pool.

Small, version-pinned pool of recent desktop Chrome UA strings, keyed by
host platform. The pool is on purpose small and static. Randomness only
kicks in once, when we pick a UA for an account. After that we save the
chosen UA and use it for the whole life of that account's session.

Why pin? Rotating an account's UA between sessions is itself a bot signal,
real browsers do not change their major version in the middle of the week.
Every InstagramAccount picks once and sticks with it. This pool just seeds
that one-time choice.

Refresh policy:
Bump the strings here when stable Chrome ships a new major version that has
at least 30% of the install base for two weeks. The UAs here should match
what chrome://settings/help shows on the matching desktop OS.
"""

from __future__ import annotations

import enum
import random
from typing import Dict, List, Optional


class Platform(str, enum.Enum):
    """OS that the fake Chromium build pretends to run on."""

    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"


# the pool.
# strings follow stable Chrome on each desktop OS. chromium only. we do
# not add Edge, Opera, Brave or mobile builds, the rest of the fingerprint
# (UA-CH, navigator.platform, GPU vendor strings) would not match and IG
# would notice.
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
    """Return one random Chrome UA from the pool for `platform`.

    Args:
        platform: Platform to pick a UA for. Accepts the Platform enum or
            its string value ("windows" / "macos" / "linux").
        rng: optional random.Random. Pass a seeded one in tests, production
            calls leave it None and use the module's shared RNG.

    Raises:
        ValueError: if `platform` is not a valid name, or the pool for the
            platform is empty (means the pool needs a refresh).
    """
    if isinstance(platform, str):
        try:
            platform = Platform(platform.lower())
        except ValueError as exc:
            raise ValueError(
                f"unknown platform {platform!r}, expected one of "
                f"{[p.value for p in Platform]}"
            ) from exc

    pool = USER_AGENT_POOL.get(platform)
    if not pool:
        raise ValueError(
            f"UA pool for platform {platform.value!r} is empty, "
            "the static pool needs a refresh"
        )

    chooser = rng if rng is not None else random
    return chooser.choice(pool)


__all__ = ["Platform", "USER_AGENT_POOL", "get_random_user_agent"]
