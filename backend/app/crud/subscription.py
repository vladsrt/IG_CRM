"""CRUD operations for ``Subscription``."""

from __future__ import annotations

import uuid
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.billing import Subscription
from app.models.user import User
from app.schemas.billing import SubscriptionCreate, SubscriptionUpdate


# ── Read ────────────────────────────────────────────────────────────────
def get_subscription(db: Session, sub_id: uuid.UUID) -> Subscription | None:
    return db.get(Subscription, sub_id)


def get_subscription_by_user(
    db: Session, user_id: uuid.UUID
) -> Subscription | None:
    stmt = select(Subscription).where(Subscription.user_id == user_id)
    return db.execute(stmt).scalar_one_or_none()


def list_subscriptions(
    db: Session, skip: int = 0, limit: int = 100
) -> Sequence[Subscription]:
    stmt = select(Subscription).offset(skip).limit(limit)
    return db.execute(stmt).scalars().all()


# ── Create ──────────────────────────────────────────────────────────────
#Create sub
def create_subscription(db: Session, sub_in: SubscriptionCreate) -> Subscription:
    if db.get(User, sub_in.user_id) is None:
        raise ValueError(f"User {sub_in.user_id} does not exist")
    if get_subscription_by_user(db, sub_in.user_id) is not None:
        raise ValueError(f"User {sub_in.user_id} already has a subscription")

    sub = Subscription(**sub_in.model_dump())
    db.add(sub)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("Could not create subscription (constraint violation)") from exc
    db.refresh(sub)
    return sub


# ── Update ──────────────────────────────────────────────────────────────
def update_subscription(
    db: Session, sub_id: uuid.UUID, sub_in: SubscriptionUpdate
) -> Subscription | None:
    sub = get_subscription(db, sub_id)
    if sub is None:
        return None

    for field, value in sub_in.model_dump(exclude_unset=True).items():
        setattr(sub, field, value)

    db.commit()
    db.refresh(sub)
    return sub


# ── Delete ──────────────────────────────────────────────────────────────
def delete_subscription(db: Session, sub_id: uuid.UUID) -> bool:
    sub = get_subscription(db, sub_id)
    if sub is None:
        return False
    db.delete(sub)
    db.commit()
    return True
