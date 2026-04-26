"""CRUD operations for ``Task``."""

from __future__ import annotations

import uuid
from typing import Any, Sequence

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.account import InstagramAccount
from app.models.task import Task, TaskStatus
from app.schemas.task import TaskCreate


class TaskUpdate(BaseModel):
    status: TaskStatus | None = None
    payload: dict[str, Any] | None = None
    priority: int | None = None
    error_log: str | None = None


# ── Read ────────────────────────────────────────────────────────────────
def get_task(db: Session, task_id: uuid.UUID) -> Task | None:
    return db.get(Task, task_id)


def list_tasks(
    db: Session,
    *,
    account_id: uuid.UUID | None = None,
    status: TaskStatus | None = None,
    skip: int = 0,
    limit: int = 100,
) -> Sequence[Task]:
    stmt = select(Task)
    if account_id is not None:
        stmt = stmt.where(Task.account_id == account_id)
    if status is not None:
        stmt = stmt.where(Task.status == status.value)
    stmt = stmt.order_by(Task.priority.desc(), Task.created_at.asc())
    stmt = stmt.offset(skip).limit(limit)
    return db.execute(stmt).scalars().all()


# ── Create ──────────────────────────────────────────────────────────────
def create_task(db: Session, task_in: TaskCreate) -> Task:
    if db.get(InstagramAccount, task_in.account_id) is None:
        raise ValueError(f"InstagramAccount {task_in.account_id} does not exist")

    payload = task_in.model_dump(mode="json")
    if isinstance(task_in.status, TaskStatus):
        payload["status"] = task_in.status.value

    task = Task(**payload)
    db.add(task)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("Could not create task (constraint violation)") from exc
    db.refresh(task)
    return task


# ── Update ──────────────────────────────────────────────────────────────
def update_task(
    db: Session, task_id: uuid.UUID, task_in: TaskUpdate
) -> Task | None:
    task = get_task(db, task_id)
    if task is None:
        return None

    data = task_in.model_dump(exclude_unset=True, mode="json")
    if "status" in data and isinstance(data["status"], TaskStatus):
        data["status"] = data["status"].value

    for field, value in data.items():
        setattr(task, field, value)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError("Could not update task (constraint violation)") from exc
    db.refresh(task)
    return task


# ── Delete ──────────────────────────────────────────────────────────────
def delete_task(db: Session, task_id: uuid.UUID) -> bool:
    task = get_task(db, task_id)
    if task is None:
        return False
    db.delete(task)
    db.commit()
    return True
