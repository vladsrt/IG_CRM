"""Spintax expansion + link obfuscation utilities.

These two helpers are deliberately co-located: every fan-out task runs them
in sequence (spin → obfuscate) so each cloned ``Task.payload`` lands with a
unique caption AND a unique URL. Same input plan, different bytes per
account — anti-fraud heuristics that match on exact-string repetition see
50 distinct posts instead of 50 copies.

Spintax syntax
~~~~~~~~~~~~~~
``"Check {out|out my} new {reel|video|drop} {today|right now}"``

* ``{a|b|c}`` — pick exactly one option uniformly at random
* Empty options are allowed: ``{|maybe}`` → 50% chance of "" or "maybe"
* Nested groups are supported recursively:
  ``"{Hello|Hi {there|y'all}}, world"``

Link obfuscation
~~~~~~~~~~~~~~~~
``obfuscate_link("https://store.example/foo")`` returns a per-call unique
short-link-style URL. The current implementation is a *placeholder* —
deterministic shape, base32 token — meant to be swapped for a real
redirect service (Bitly / Rebrandly / self-hosted) by changing one
function. The signature stays stable.
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


# ── Spintax ─────────────────────────────────────────────────────────────
class SpintaxError(ValueError):
    """Raised when a spintax string is malformed (unbalanced braces)."""


_SPINTAX_OPEN = "{"
_SPINTAX_CLOSE = "}"
_SPINTAX_SEP = "|"
_MAX_RECURSION_DEPTH = 32


_SPINTAX_GROUP_RE = re.compile(r"\{[^{}]*\|[^{}]*\}")


def has_spintax(text: str) -> bool:
    """Cheap pre-check: does ``text`` contain a resolvable spintax group?

    A "resolvable group" is ``{...|...}`` — at least one ``|`` between
    matched braces. Bare ``{username}`` placeholders and unbalanced braces
    return ``False`` so we leave them alone.
    """
    return _SPINTAX_GROUP_RE.search(text) is not None


def spin(text: str, *, rng: Optional[random.Random] = None) -> str:
    """Pick one variant from a spintax-encoded string.

    Args:
        text: The spintax source. Plain text without ``{...}`` is returned
            unchanged.
        rng: Optional ``random.Random`` for deterministic tests. Production
            callers omit this and get the module's seeded RNG.

    Raises:
        SpintaxError: On unbalanced braces.
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

    # Find the FIRST innermost `{...|...}` group: a `{` followed only by
    # non-brace characters that includes at least one `|`, then a matching
    # `}`. Requiring the `|` means `{username}` (downstream templating
    # placeholders) is left untouched. Resolving innermost-first handles
    # nested groups in O(n*depth) without a real parser.
    pattern = re.compile(r"\{([^{}]*\|[^{}]*)\}")
    while True:
        match = pattern.search(text)
        if match is None:
            break
        options = match.group(1).split(_SPINTAX_SEP)
        chosen = rng.choice(options)
        text = text[: match.start()] + chosen + text[match.end() :]

    # If the first pass introduced new groups (a chosen option itself
    # contained spintax), recurse.
    if has_spintax(text):
        return _spin_recursive(text, rng, _depth=_depth + 1)
    return text


# ── Link obfuscation (Task 8.3) ─────────────────────────────────────────
_OBFUSCATOR_BASE_URL: str = os.environ.get(
    "LINK_OBFUSCATOR_BASE_URL", "https://lnk.ig-crm.local/r"
)
_OBFUSCATOR_TOKEN_BYTES: int = 8


def obfuscate_link(target_url: str) -> str:
    """Return a per-call unique short-link-style URL pointing at ``target_url``.

    Placeholder implementation — the production wiring should:

      1. POST ``target_url`` to a real redirect service (Bitly, Rebrandly,
         self-hosted ``r/<token>``).
      2. Persist ``(token, target_url, account_id, task_id)`` so click
         analytics can be attributed back to the dispatching account.
      3. Return the public short URL.

    The signature ``(target_url: str) -> str`` is stable; only the body
    changes when wiring the real service.
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


# ── Combined helpers used by the orchestrator ───────────────────────────
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def uniqueize_text(text: str, *, rng: Optional[random.Random] = None) -> str:
    """Apply spintax expansion AND link obfuscation to one piece of text.

    Order matters: spin first (spintax may itself contain URLs as options),
    then obfuscate every URL the resolved text contains.
    """
    if not text:
        return text
    spun = spin(text, rng=rng)
    return _URL_RE.sub(lambda m: obfuscate_link(m.group(0)), spun)


def uniqueize_args(
    args: dict[str, object], *, rng: Optional[random.Random] = None
) -> dict[str, object]:
    """Return a shallow copy of ``args`` with text-bearing values uniqueized.

    Any string value gets passed through ``uniqueize_text``. Non-string
    values are returned unchanged. Nested dicts / lists are NOT recursed
    into — caption-style payloads are flat by convention.
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
    """Return a shallow-cloned plan payload with every command's args spun.

    Each call gets its own seeded RNG (or a fresh one if ``seed`` is
    ``None``) so two clones of the same plan produce different output.
    Used by the fan-out endpoint — one call per dispatched account.
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

    # Stamp a per-clone uniqueization id for debugging / audit trails.
    out["_uniqueization_id"] = uuid.uuid4().hex
    return out
