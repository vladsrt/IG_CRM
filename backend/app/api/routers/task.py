"""Task routes. JWT-protected and scoped to the caller's accounts."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.core.database import get_db
from app.crud import task as crud_task
from app.crud.task import TaskUpdate
from app.models.account import InstagramAccount
from app.models.task import Task, TaskStatus
from app.models.user import User
from app.schemas.task import TaskCreate, TaskRead

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _user_account_ids(db: Session, user: User) -> list[uuid.UUID]:
    return list(
        db.execute(
            select(InstagramAccount.id).where(InstagramAccount.user_id == user.id)
        ).scalars().all()
    )


def _owned_task_or_404(db: Session, task_id: uuid.UUID, user: User) -> Task:
    task = crud_task.get_task(db, task_id)
    if task is None or task.account_id not in set(_user_account_ids(db, user)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


@router.post("/", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def create_task(
    task_in: TaskCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TaskRead:
    if task_in.account_id not in set(_user_account_ids(db, current_user)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    try:
        task = crud_task.create_task(db, task_in)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return TaskRead.model_validate(task)


@router.get("/", response_model=list[TaskRead])
def list_tasks(
    account_id: uuid.UUID | None = Query(default=None),
    task_status: TaskStatus | None = Query(default=None, alias="status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TaskRead]:
    owned = _user_account_ids(db, current_user)
    if not owned:
        return []
    stmt = select(Task).where(Task.account_id.in_(owned))
    if account_id is not None:
        if account_id not in set(owned):
            return []
        stmt = stmt.where(Task.account_id == account_id)
    if task_status is not None:
        stmt = stmt.where(Task.status == task_status.value)
    stmt = stmt.order_by(Task.created_at.desc()).offset(skip).limit(limit)
    rows = db.execute(stmt).scalars().all()
    return [TaskRead.model_validate(t) for t in rows]


@router.get("/{task_id}", response_model=TaskRead)
def get_task(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TaskRead:
    return TaskRead.model_validate(_owned_task_or_404(db, task_id, current_user))


@router.patch("/{task_id}", response_model=TaskRead)
def update_task(
    task_id: uuid.UUID,
    task_in: TaskUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TaskRead:
    _owned_task_or_404(db, task_id, current_user)
    try:
        task = crud_task.update_task(db, task_id, task_in)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return TaskRead.model_validate(task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(
    task_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    _owned_task_or_404(db, task_id, current_user)
    crud_task.delete_task(db, task_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
