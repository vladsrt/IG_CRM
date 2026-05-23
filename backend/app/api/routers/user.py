"""User and subscription routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.dependencies import require_admin
from app.core.database import get_db
from app.crud import subscription as crud_subscription
from app.crud import user as crud_user
from app.crud.user import UserUpdate
from app.schemas.billing import SubscriptionRead, SubscriptionUpdate
from app.schemas.user import UserCreate, UserRead

# Admin-only: managing users and changing tiers must not be self-service
# (a regular user could otherwise PATCH their own subscription to enterprise).
# Public signup goes through /auth/register; self profile through /auth/me.
router = APIRouter(
    prefix="/users", tags=["users"], dependencies=[Depends(require_admin)]
)


@router.post(
    "/",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new user, also makes a free subscription",
)
def create_user(
    user_in: UserCreate,
    db: Session = Depends(get_db),
) -> UserRead:
    try:
        user = crud_user.create_user(db, user_in)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    return UserRead.model_validate(user)


@router.get("/", response_model=list[UserRead])
def list_users(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[UserRead]:
    rows = crud_user.list_users(db, skip=skip, limit=limit)
    return [UserRead.model_validate(u) for u in rows]


@router.get("/{user_id}", response_model=UserRead)
def get_user(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> UserRead:
    user = crud_user.get_user(db, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    return UserRead.model_validate(user)


@router.patch("/{user_id}", response_model=UserRead)
def update_user(
    user_id: uuid.UUID,
    user_in: UserUpdate,
    db: Session = Depends(get_db),
) -> UserRead:
    try:
        user = crud_user.update_user(db, user_id, user_in)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    return UserRead.model_validate(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> Response:
    if not crud_user.delete_user(db, user_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# nested subscription routes
@router.get(
    "/{user_id}/subscription",
    response_model=SubscriptionRead,
    summary="Get current subscription of the user",
)
def get_user_subscription(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> SubscriptionRead:
    sub = crud_subscription.get_subscription_by_user(db, user_id)
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found for this user",
        )
    return SubscriptionRead.model_validate(sub)


@router.patch(
    "/{user_id}/subscription",
    response_model=SubscriptionRead,
    summary="Update user subscription, like a tier upgrade",
)
def update_user_subscription(
    user_id: uuid.UUID,
    sub_in: SubscriptionUpdate,
    db: Session = Depends(get_db),
) -> SubscriptionRead:
    sub = crud_subscription.get_subscription_by_user(db, user_id)
    if sub is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found for this user",
        )
    updated = crud_subscription.update_subscription(db, sub.id, sub_in)
    if updated is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Subscription not found"
        )
    return SubscriptionRead.model_validate(updated)
