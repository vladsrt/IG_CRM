"""CRUD for InstagramAccount."""

from __future__ import annotations

import uuid
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.secrets import encrypt_cookies, encrypt_secret
from app.models.account import AuthMethod, InstagramAccount, Platform
from app.models.proxy import Proxy
from app.models.user import User
from app.schemas.account import InstagramAccountCreate, InstagramAccountUpdate
from workers.utils.ua_generator import get_random_user_agent

# re-export for old imports. The real home is now app.schemas.account, but
# some old code still does: from app.crud.account import InstagramAccountUpdate.
__all__ = [
    "InstagramAccountUpdate",
    "create_account",
    "delete_account",
    "get_account",
    "list_accounts",
    "list_accounts_by_tags",
    "update_account",
]


def _normalize_tags(tags: list[str] | None) -> list[str]:
    """Lowercase, strip and dedupe tags so the jsonb column stays clean."""
    if not tags:
        return []
    seen: dict[str, None] = {}
    for raw in tags:
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip().lower()
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen.keys())


# read
def get_account(db: Session, account_id: uuid.UUID) -> InstagramAccount | None:
    return db.get(InstagramAccount, account_id)


def list_accounts(
    db: Session,
    *,
    user_id: uuid.UUID | None = None,
    tags: list[str] | None = None,
    skip: int = 0,
    limit: int = 100,
) -> Sequence[InstagramAccount]:
    stmt = select(InstagramAccount)
    if user_id is not None:
        stmt = stmt.where(InstagramAccount.user_id == user_id)
    if tags:
        # jsonb contains: row.tags must include every requested tag.
        wanted = _normalize_tags(tags)
        if wanted:
            stmt = stmt.where(InstagramAccount.tags.contains(wanted))
    stmt = stmt.offset(skip).limit(limit)
    return db.execute(stmt).scalars().all()


def list_accounts_by_tags(
    db: Session,
    tags: list[str],
    *,
    user_id: uuid.UUID | None = None,
) -> Sequence[InstagramAccount]:
    """Shortcut used by the AI orchestrator."""
    return list_accounts(db, user_id=user_id, tags=tags, limit=10_000)


# create
def create_account(
    db: Session, account_in: InstagramAccountCreate
) -> InstagramAccount:
    if db.get(User, account_in.user_id) is None:
        raise ValueError(f"User {account_in.user_id} does not exist")
    if account_in.proxy_id is not None and db.get(Proxy, account_in.proxy_id) is None:
        raise ValueError(f"Proxy {account_in.proxy_id} does not exist")

    payload = account_in.model_dump(mode="json")
    payload["auth_method"] = (
        account_in.auth_method.value
        if isinstance(account_in.auth_method, AuthMethod)
        else account_in.auth_method
    )
    payload["tags"] = _normalize_tags(payload.get("tags"))

    # encrypt secrets at rest (Fernet). decrypted only in the worker.
    payload["ig_password"] = encrypt_secret(payload.get("ig_password"))
    payload["cookies"] = encrypt_cookies(payload.get("cookies"))

    # pick and save a UA at create time. if the user gave none, take one
    # from the pool that matches the chosen platform. once saved, the UA is
    # not rotated. swapping the UA later is a bigger bot signal than just
    # ending up with a not-so-great string.
    platform_raw = payload.get("platform") or Platform.WINDOWS.value
    payload["platform"] = (
        platform_raw.value if isinstance(platform_raw, Platform) else platform_raw
    )
    if not payload.get("user_agent"):
        payload["user_agent"] = get_random_user_agent(payload["platform"])

    account = InstagramAccount(**payload)
    db.add(account)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("Could not create account (constraint violation)") from exc
    db.refresh(account)
    return account

# update
def update_account(
    db: Session,
    account_id: uuid.UUID,
    account_in: InstagramAccountUpdate,
) -> InstagramAccount | None:
    account = get_account(db, account_id)
    if account is None:
        return None

    data = account_in.model_dump(exclude_unset=True, mode="json")
    if "proxy_id" in data and data["proxy_id"] is not None:
        if db.get(Proxy, data["proxy_id"]) is None:
            raise ValueError(f"Proxy {data['proxy_id']} does not exist")
    if "auth_method" in data and isinstance(data["auth_method"], AuthMethod):
        data["auth_method"] = data["auth_method"].value
    if "platform" in data and isinstance(data["platform"], Platform):
        data["platform"] = data["platform"].value
    if "tags" in data:
        data["tags"] = _normalize_tags(data["tags"])
    # re-encrypt secrets if they are being updated
    if "ig_password" in data:
        data["ig_password"] = encrypt_secret(data["ig_password"])
    if "cookies" in data:
        data["cookies"] = encrypt_cookies(data["cookies"])

    for field, value in data.items():
        setattr(account, field, value)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("Could not update account (constraint violation)") from exc
    db.refresh(account)
    return account


# delete
def delete_account(db: Session, account_id: uuid.UUID) -> bool:
    account = get_account(db, account_id)
    if account is None:
        return False
    db.delete(account)
    db.commit()
    return True
