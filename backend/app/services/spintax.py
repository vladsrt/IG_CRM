"""Spintax expand and link obfuscate helpers.

These two live together because every fan-out task runs them in order
(spin then obfuscate), so each cloned Task.payload ends up with its own
caption and its own URL. Same input plan, different bytes per account.
Anti-fraud checks that match on exact-string repeats see 50 different
posts instead of 50 copies.

Spintax syntax:
    "Check {out|out my} new {reel|video|drop} {today|right now}"

    {a|b|c}      pick one option at random
    {|maybe}     empty option allowed, 50/50 between "" and "maybe"
    nested too:  "{Hello|Hi {there|y'all}}, world"

Link obfuscation:
    obfuscate_link("https://store.example/foo") gives back a per-call
    unique short-link-style URL. The current code is a stub: fixed shape,
    base32 token. Swap one function later for a real redirect (Bitly,
    Rebrandly, self-hosted...). The signature stays the same.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import random
import re
import secrets
import uuid
from typing import Optional

logger = logging.getLogger(__name__)


# spintax
class SpintaxError(ValueError):
    """Raised when the spintax string is broken (braces do not match)."""


_SPINTAX_OPEN = "{"
_SPINTAX_CLOSE = "}"
_SPINTAX_SEP = "|"
_MAX_RECURSION_DEPTH = 32


_SPINTAX_GROUP_RE = re.compile(r"\{[^{}]*\|[^{}]*\}")


def has_spintax(text: str) -> bool:
    """Quick check: does text have a real spintax group?

    A real group is `{...|...}`, at least one `|` between matching braces.
    Bare `{username}` placeholders and unmatched braces return False, so we
    leave them alone.
    """
    return _SPINTAX_GROUP_RE.search(text) is not None


def spin(text: str, *, rng: Optional[random.Random] = None) -> str:
    """Pick one variant from a spintax string.

    Args:
        text: spintax source. Plain text without `{...}` is returned as is.
        rng: optional random.Random for tests where output must be fixed.
            Production code skips this and gets the module's RNG.

    Raises:
        SpintaxError: when braces do not balance.
    """
    if not text or not has_spintax(text):
        return text
    rng = rng or random.SystemRandom()
    return _spin_recursive(text, rng, _depth=0)


def _spin_recursive(text: str, rng: random.Random, *, _depth: int) -> str:
    if _depth > _MAX_RECURSION_DEPTH:
        raise SpintaxError(
            f"Spintax exceeded max recursion depth ({_MAX_RECURSION_DEPTH}); "
            "likely cyclic or pathological input."
        )

    # find the first innermost `{...|...}` group: `{`, then only non-brace
    # chars that include at least one `|`, then a matching `}`. requiring
    # the `|` keeps `{username}` placeholders intact. resolving innermost
    # first handles nested groups in O(n*depth) without a real parser.
    pattern = re.compile(r"\{([^{}]*\|[^{}]*)\}")
    while True:
        match = pattern.search(text)
        if match is None:
            break
        options = match.group(1).split(_SPINTAX_SEP)
        chosen = rng.choice(options)
        text = text[: match.start()] + chosen + text[match.end() :]

    # if a chosen option contained more spintax, run again.
    if has_spintax(text):
        return _spin_recursive(text, rng, _depth=_depth + 1)
    return text


# link obfuscation
_OBFUSCATOR_BASE_URL: str = os.environ.get(
    "LINK_OBFUSCATOR_BASE_URL", "https://lnk.ig-crm.local/r"
)
_OBFUSCATOR_TOKEN_BYTES: int = 8


def obfuscate_link(target_url: str) -> str:
    """Return a per-call unique short-link-style URL that points at target_url.

    Stub. Real wiring should:

      1. POST `target_url` to a real redirect service (Bitly, Rebrandly,
         self-hosted r/<token>).
      2. Save (token, target_url, account_id, task_id) so click analytics
         can be tied back to the account that sent it.
      3. Return the public short url.

    The signature `(target_url: str) -> str` does not change, only the
    body changes when the real service is wired in.
    """
    if not target_url or not isinstance(target_url, str):
        raise ValueError("obfuscate_link requires a non-empty string URL")

    token = (
        base64.b32encode(secrets.token_bytes(_OBFUSCATOR_TOKEN_BYTES))
        .rstrip(b"=")
        .decode("ascii")
        .lower()
    )
    digest = hashlib.sha1(target_url.encode("utf-8")).hexdigest()[:6]
    return f"{_OBFUSCATOR_BASE_URL}/{token}{digest}"


# helpers used by the orchestrator
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def uniqueize_text(text: str, *, rng: Optional[random.Random] = None) -> str:
    """Run spintax expand and link obfuscate on one piece of text.

    Order matters here. We spin first, because a spintax option can itself
    be a URL, then we obfuscate every URL that ends up in the result.
    """
    if not text:
        return text
    spun = spin(text, rng=rng)
    return _URL_RE.sub(lambda m: obfuscate_link(m.group(0)), spun)


def uniqueize_args(
    args: dict[str, object], *, rng: Optional[random.Random] = None
) -> dict[str, object]:
    """Return a shallow copy of args with string values uniqueized.

    Every string value goes through `uniqueize_text`. Non-string values
    are kept as is. We do not recurse into nested dicts or lists, the
    caption payloads are always flat.
    """
    out: dict[str, object] = {}
    for key, value in args.items():
        if isinstance(value, str):
            out[key] = uniqueize_text(value, rng=rng)
        else:
            out[key] = value
    return out


def uniqueize_plan_payload(
    payload: dict[str, object],
    *,
    seed: Optional[int] = None,
) -> dict[str, object]:
    """Return a shallow copy of the plan with every command's args spun.

    Each call gets its own seeded RNG (or a fresh one when seed is None),
    so two clones of the same plan come out different. Used by the fan-out
    route, once per dispatched account.
    """
    rng = random.Random(seed) if seed is not None else random.SystemRandom()

    out = dict(payload)
    raw_commands = payload.get("commands")
    if not isinstance(raw_commands, list):
        return out

    new_commands: list[dict[str, object]] = []
    for command in raw_commands:
        if not isinstance(command, dict):
            new_commands.append(command)  # type: ignore[arg-type]
            continue
        new_cmd = dict(command)
        args = new_cmd.get("args")
        if isinstance(args, dict):
            new_cmd["args"] = uniqueize_args(args, rng=rng)
        new_commands.append(new_cmd)
    out["commands"] = new_commands

    # stamp a per-clone id so we can trace it in logs later
    out["_uniqueization_id"] = uuid.uuid4().hex
    return out
