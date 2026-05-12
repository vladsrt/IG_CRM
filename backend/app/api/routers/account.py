"""instagram account routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.crud import account as crud_account
from app.schemas.account import (
    InstagramAccountCreate,
    InstagramAccountRead,
    InstagramAccountUpdate,
)

# uncomment these to protect routes with jwt auth:
# from app.api.dependencies import get_current_user
# from app.models.user import User

router = APIRouter(prefix="/accounts", tags=["instagram-accounts"])


@router.post(
    "/",
    response_model=InstagramAccountRead,
    status_code=status.HTTP_201_CREATED,
)
def create_account(
    account_in: InstagramAccountCreate,
    db: Session = Depends(get_db),
) -> InstagramAccountRead:
    try:
        account = crud_account.create_account(db, account_in)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return InstagramAccountRead.model_validate(account)


@router.get("/", response_model=list[InstagramAccountRead])
def list_accounts(
    user_id: uuid.UUID | None = Query(default=None),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[InstagramAccountRead]:
    rows = crud_account.list_accounts(db, user_id=user_id, skip=skip, limit=limit)
    return [InstagramAccountRead.model_validate(a) for a in rows]


# --- example: how to protect this route with jwt ---
# replace list_accounts above with this to enforce multi-tenancy:
#
# @router.get("/", response_model=list[InstagramAccountRead])
# def list_accounts(
#     skip: int = Query(0, ge=0),
#     limit: int = Query(100, ge=1, le=500),
#     current_user: User = Depends(get_current_user),
#     db: Session = Depends(get_db),
# ) -> list[InstagramAccountRead]:
#     rows = crud_account.list_accounts(
#         db, user_id=current_user.id, skip=skip, limit=limit,
#     )
#     return [InstagramAccountRead.model_validate(a) for a in rows]


@router.get("/{account_id}", response_model=InstagramAccountRead)
def get_account(
    account_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> InstagramAccountRead:
    account = crud_account.get_account(db, account_id)
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="InstagramAccount not found",
        )
    return InstagramAccountRead.model_validate(account)


@router.patch("/{account_id}", response_model=InstagramAccountRead)
def update_account(
    account_id: uuid.UUID,
    account_in: InstagramAccountUpdate,
    db: Session = Depends(get_db),
) -> InstagramAccountRead:
    try:
        account = crud_account.update_account(db, account_id, account_in)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="InstagramAccount not found",
        )
    return InstagramAccountRead.model_validate(account)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_account(
    account_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> Response:
    if not crud_account.delete_account(db, account_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="InstagramAccount not found",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
