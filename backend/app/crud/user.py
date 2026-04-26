"""CRUD operations for ``User`` (with auto-provisioned free Subscription)."""

from __future__ import annotations

import uuid
from typing import Sequence

from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models.billing import Subscription
from app.models.user import User
from app.schemas.user import UserCreate


class UserUpdate(BaseModel):
    """Fields that may be patched on an existing user."""

    email: EmailStr | None = None
    password: str | None = Field(default=None, min_length=8)


# ── Read ────────────────────────────────────────────────────────────────
def get_user(db: Session, user_id: uuid.UUID) -> User | None:
    return db.get(User, user_id)


def get_user_by_email(db: Session, email: str) -> User | None:
    stmt = select(User).where(User.email == email)
    return db.execute(stmt).scalar_one_or_none()


def list_users(db: Session, skip: int = 0, limit: int = 100) -> Sequence[User]:
    stmt = select(User).order_by(User.created_at.desc()).offset(skip).limit(limit)
    return db.execute(stmt).scalars().all()


# ── Create ──────────────────────────────────────────────────────────────
def create_user(db: Session, user_in: UserCreate) -> User:
    """Create a user and atomically attach a default ``free`` Subscription."""
    if get_user_by_email(db, user_in.email) is not None:
        raise ValueError(f"User with email {user_in.email!r} already exists")

    user = User(
        email=user_in.email,
        hashed_password=hash_password(user_in.password),
    )
    user.subscription = Subscription(tier="free", is_active=True)

    db.add(user)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("Could not create user (constraint violation)") from exc
    db.refresh(user)
    return user


# ── Update ──────────────────────────────────────────────────────────────
def update_user(
    db: Session, user_id: uuid.UUID, user_in: UserUpdate
) -> User | None:
    user = get_user(db, user_id)
    if user is None:
        return None

    data = user_in.model_dump(exclude_unset=True)
    if "password" in data:
        user.hashed_password = hash_password(data.pop("password"))
    if "email" in data:
        user.email = data["email"]

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("Could not update user (constraint violation)") from exc
    db.refresh(user)
    return user


# ── Delete ──────────────────────────────────────────────────────────────
def delete_user(db: Session, user_id: uuid.UUID) -> bool:
    user = get_user(db, user_id)
    if user is None:
        return False
    db.delete(user)
    db.commit()
    return True
