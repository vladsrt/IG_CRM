"""Task endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.crud import task as crud_task
from app.crud.task import TaskUpdate
from app.models.task import TaskStatus
from app.schemas.task import TaskCreate, TaskRead

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.post(
    "/",
    response_model=TaskRead,
    status_code=status.HTTP_201_CREATED,
)
def create_task(
    task_in: TaskCreate,
    db: Session = Depends(get_db),
) -> TaskRead:
    try:
        task = crud_task.create_task(db, task_in)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return TaskRead.model_validate(task)


@router.get("/", response_model=list[TaskRead])
def list_tasks(
    account_id: uuid.UUID | None = Query(default=None),
    task_status: TaskStatus | None = Query(default=None, alias="status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[TaskRead]:
    rows = crud_task.list_tasks(
        db,
        account_id=account_id,
        status=task_status,
        skip=skip,
        limit=limit,
    )
    return [TaskRead.model_validate(t) for t in rows]


@router.get("/{task_id}", response_model=TaskRead)
def get_task(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> TaskRead:
    task = crud_task.get_task(db, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )
    return TaskRead.model_validate(task)


@router.patch("/{task_id}", response_model=TaskRead)
def update_task(
    task_id: uuid.UUID,
    task_in: TaskUpdate,
    db: Session = Depends(get_db),
) -> TaskRead:
    try:
        task = crud_task.update_task(db, task_id, task_in)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )
    return TaskRead.model_validate(task)


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> Response:
    if not crud_task.delete_task(db, task_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
