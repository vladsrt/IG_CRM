"""instagram account routes. JWT-protected and scoped to the owner."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.core.database import get_db
from app.crud import account as crud_account
from app.models.account import InstagramAccount
from app.models.user import User
from app.schemas.account import (
    InstagramAccountCreate,
    InstagramAccountRead,
    InstagramAccountUpdate,
)

router = APIRouter(prefix="/accounts", tags=["instagram-accounts"])


def _to_read(account: InstagramAccount) -> InstagramAccountRead:
    """Serialize an account, adding non-secret has_cookies/has_password flags."""
    read = InstagramAccountRead.model_validate(account)
    read.has_cookies = account.cookies is not None
    read.has_password = bool(account.ig_password)
    return read


def _owned_or_404(
    db: Session, account_id: uuid.UUID, user: User
) -> InstagramAccount:
    """Fetch an account and confirm it belongs to the caller (IDOR guard)."""
    account = crud_account.get_account(db, account_id)
    if account is None or account.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="InstagramAccount not found",
        )
    return account


@router.post(
    "/",
    response_model=InstagramAccountRead,
    status_code=status.HTTP_201_CREATED,
)
def create_account(
    account_in: InstagramAccountCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InstagramAccountRead:
    # the owner always comes from the token, never trust the body
    account_in.user_id = current_user.id
    try:
        account = crud_account.create_account(db, account_in)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return _to_read(account)


@router.get("/", response_model=list[InstagramAccountRead])
def list_accounts(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[InstagramAccountRead]:
    rows = crud_account.list_accounts(
        db, user_id=current_user.id, skip=skip, limit=limit,
    )
    return [_to_read(a) for a in rows]


@router.get("/{account_id}", response_model=InstagramAccountRead)
def get_account(
    account_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InstagramAccountRead:
    return _to_read(_owned_or_404(db, account_id, current_user))


@router.patch("/{account_id}", response_model=InstagramAccountRead)
def update_account(
    account_id: uuid.UUID,
    account_in: InstagramAccountUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InstagramAccountRead:
    _owned_or_404(db, account_id, current_user)
    try:
        account = crud_account.update_account(db, account_id, account_in)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return _to_read(account)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    account_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    _owned_or_404(db, account_id, current_user)
    crud_account.delete_account(db, account_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
