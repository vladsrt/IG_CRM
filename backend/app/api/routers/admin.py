"""Admin routes: server capacity + per-user plan/agent management.

Every route requires an admin (email in ADMIN_EMAILS).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import require_admin
from app.core.database import get_db
from app.crud import subscription as crud_subscription
from app.models.account import InstagramAccount
from app.models.task import Task, TaskStatus
from app.models.user import User
from app.services.agents import agents_for_subscription
from app.services.capacity import snapshot

router = APIRouter(
    prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)]
)


class AdminUserRow(BaseModel):
    id: uuid.UUID
    email: str
    created_at: datetime
    tier: str
    agents_limit: int
    accounts_count: int
    running_tasks: int


class AdminUserUpdate(BaseModel):
    tier: str | None = None
    agents_limit: int | None = None


def _row_for(db: Session, user: User) -> AdminUserRow:
    sub = user.subscription
    accounts_count = int(
        db.execute(
            select(func.count(InstagramAccount.id)).where(
                InstagramAccount.user_id == user.id
            )
        ).scalar_one()
    )
    running = int(
        db.execute(
            select(func.count(Task.id))
            .select_from(Task)
            .join(InstagramAccount, Task.account_id == InstagramAccount.id)
            .where(
                InstagramAccount.user_id == user.id,
                Task.status == TaskStatus.RUNNING.value,
            )
        ).scalar_one()
    )
    return AdminUserRow(
        id=user.id,
        email=user.email,
        created_at=user.created_at,
        tier=sub.tier if sub else "free",
        agents_limit=agents_for_subscription(sub),
        accounts_count=accounts_count,
        running_tasks=running,
    )


@router.get("/capacity")
def get_capacity() -> dict:
    """Live host load (CPU/RAM + our browser count) for the admin panel."""
    return snapshot()


@router.get("/users", response_model=list[AdminUserRow])
def list_users(db: Session = Depends(get_db)) -> list[AdminUserRow]:
    users = db.execute(select(User).order_by(User.created_at.asc())).scalars().all()
    return [_row_for(db, u) for u in users]


@router.patch("/users/{user_id}", response_model=AdminUserRow)
def update_user_plan(
    user_id: uuid.UUID,
    body: AdminUserUpdate,
    db: Session = Depends(get_db),
) -> AdminUserRow:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    sub = crud_subscription.get_subscription_by_user(db, user_id)
    if sub is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found")

    if body.tier is not None:
        if body.tier not in ("free", "pro", "enterprise"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid tier")
        sub.tier = body.tier
    if body.agents_limit is not None:
        sub.agents_limit = max(0, body.agents_limit)

    db.commit()
    db.refresh(user)
    return _row_for(db, user)
