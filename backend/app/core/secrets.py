"""Application-layer encryption for account secrets (cookies, ig_password).

Why: those fields used to be stored in Postgres in plaintext AND returned by the
API. Anyone with a DB dump or an over-broad API response could harvest live
Instagram sessions. We now Fernet-encrypt them at the CRUD boundary and only
decrypt inside the worker right before a browser session needs them.

Key: derived from ``settings.SECRET_KEY`` (SHA-256 -> 32 bytes -> urlsafe b64),
so there is no second secret to manage. Rotating SECRET_KEY makes existing
ciphertext unreadable — acceptable for this stage (re-enter the secrets), and
``decrypt_*`` fails open to the stored value so a rotation never crashes a run.

Back-compat: values without the ``enc::`` marker are treated as legacy plaintext
and returned as-is, so rows written before this change keep working.
"""

from __future__ import annotations

import base64
import hashlib
import json
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

_PREFIX = "enc::"


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    digest = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext: str | None) -> str | None:
    """Encrypt a short string (e.g. ig_password). Empty/None pass through."""
    if not plaintext:
        return plaintext
    token = _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")
    return _PREFIX + token


def decrypt_secret(value: str | None) -> str | None:
    """Decrypt a value produced by :func:`encrypt_secret`. Fail open."""
    if not value or not value.startswith(_PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(_PREFIX):].encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return value


def encrypt_cookies(cookies: Any) -> dict[str, str] | None:
    """Encrypt the cookies blob into a JSONB-safe ``{"_enc": "..."}`` envelope."""
    if cookies is None:
        return None
    raw = json.dumps(cookies)
    token = _fernet().encrypt(raw.encode("utf-8")).decode("utf-8")
    return {"_enc": _PREFIX + token}


def decrypt_cookies(stored: Any) -> Any:
    """Reverse :func:`encrypt_cookies`. Legacy plaintext cookies pass through."""
    if isinstance(stored, dict) and "_enc" in stored:
        token = stored["_enc"]
        if isinstance(token, str) and token.startswith(_PREFIX):
            token = token[len(_PREFIX):]
        try:
            return json.loads(_fernet().decrypt(token.encode("utf-8")).decode("utf-8"))
        except (InvalidToken, ValueError):
            return stored
    return stored
