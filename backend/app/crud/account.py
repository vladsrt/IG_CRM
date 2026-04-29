"""CRUD operations for ``InstagramAccount``."""

from __future__ import annotations

import uuid
from typing import Any, Sequence

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.account import AuthMethod, InstagramAccount
from app.models.proxy import Proxy
from app.models.user import User
from app.schemas.account import InstagramAccountCreate


class InstagramAccountUpdate(BaseModel):
    ig_username: str | None = None
    ig_password: str | None = None
    auth_method: AuthMethod | None = None
    proxy_id: uuid.UUID | None = None
    proxy_session_id: str | None = None
    cookies: dict[str, Any] | None = None
    status: str | None = None
    error_log: str | None = None
    tags: list[str] | None = Field(default=None)


def _normalize_tags(tags: list[str] | None) -> list[str]:
    """Lowercase, strip, dedupe — keeps the JSONB column tidy."""
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


# ── Read ────────────────────────────────────────────────────────────────
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
        # JSONB containment: row.tags must contain every requested tag.
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
    """Convenience helper used by the AI orchestrator (Sprint 5)."""
    return list_accounts(db, user_id=user_id, tags=tags, limit=10_000)


# ── Create ──────────────────────────────────────────────────────────────
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
# ── Update ──────────────────────────────────────────────────────────────
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
    if "tags" in data:
        data["tags"] = _normalize_tags(data["tags"])

    for field, value in data.items():
        setattr(account, field, value)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("Could not update account (constraint violation)") from exc
    db.refresh(account)
    return account


# ── Delete ──────────────────────────────────────────────────────────────
def delete_account(db: Session, account_id: uuid.UUID) -> bool:
    account = get_account(db, account_id)
    if account is None:
        return False
    db.delete(account)
    db.commit()
    return True
