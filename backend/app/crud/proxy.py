"""CRUD operations for ``Proxy``."""

from __future__ import annotations

import uuid
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.proxy import Proxy
from app.schemas.proxy import ProxyCreate, ProxyUpdate

# Backwards-compat re-export — ``ProxyUpdate`` now lives in
# ``app.schemas.proxy`` (canonical home), but legacy imports keep working.
__all__ = [
    "ProxyUpdate",
    "create_proxy",
    "delete_proxy",
    "get_proxy",
    "list_proxies",
    "update_proxy",
]


# ── Read ────────────────────────────────────────────────────────────────
def get_proxy(db: Session, proxy_id: uuid.UUID) -> Proxy | None:
    return db.get(Proxy, proxy_id)

def list_proxies(db: Session, skip: int = 0, limit: int = 100) -> Sequence[Proxy]:
    stmt = select(Proxy).offset(skip).limit(limit)
    return db.execute(stmt).scalars().all()


# ── Create ──────────────────────────────────────────────────────────────
def create_proxy(db: Session, proxy_in: ProxyCreate) -> Proxy:
    proxy = Proxy(**proxy_in.model_dump(mode="json"))
    db.add(proxy)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("Could not create proxy (constraint violation)") from exc
    db.refresh(proxy)
    return proxy


# ── Update ──────────────────────────────────────────────────────────────
def update_proxy(
    db: Session, proxy_id: uuid.UUID, proxy_in: ProxyUpdate
) -> Proxy | None:
    proxy = get_proxy(db, proxy_id)
    if proxy is None:
        return None

    data = proxy_in.model_dump(exclude_unset=True, mode="json")
    for field, value in data.items():
        setattr(proxy, field, value)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("Could not update proxy (constraint violation)") from exc
    db.refresh(proxy)
    return proxy


# ── Delete ──────────────────────────────────────────────────────────────
def delete_proxy(db: Session, proxy_id: uuid.UUID) -> bool:
    proxy = get_proxy(db, proxy_id)
    if proxy is None:
        return False
    db.delete(proxy)
    db.commit()
    return True
