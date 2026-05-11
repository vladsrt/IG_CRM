"""Proxy routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.crud import proxy as crud_proxy
from app.crud.proxy import ProxyUpdate
from app.schemas.proxy import ProxyCreate, ProxyRead

router = APIRouter(prefix="/proxies", tags=["proxies"])


@router.post(
    "/",
    response_model=ProxyRead,
    status_code=status.HTTP_201_CREATED,
)
def create_proxy(
    proxy_in: ProxyCreate,
    db: Session = Depends(get_db),
) -> ProxyRead:
    try:
        proxy = crud_proxy.create_proxy(db, proxy_in)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return ProxyRead.model_validate(proxy)


@router.get("/", response_model=list[ProxyRead])
def list_proxies(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[ProxyRead]:
    rows = crud_proxy.list_proxies(db, skip=skip, limit=limit)
    return [ProxyRead.model_validate(p) for p in rows]


@router.get("/{proxy_id}", response_model=ProxyRead)
def get_proxy(
    proxy_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> ProxyRead:
    proxy = crud_proxy.get_proxy(db, proxy_id)
    if proxy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Proxy not found"
        )
    return ProxyRead.model_validate(proxy)


@router.patch("/{proxy_id}", response_model=ProxyRead)
def update_proxy(
    proxy_id: uuid.UUID,
    proxy_in: ProxyUpdate,
    db: Session = Depends(get_db),
) -> ProxyRead:
    try:
        proxy = crud_proxy.update_proxy(db, proxy_id, proxy_in)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    if proxy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Proxy not found"
        )
    return ProxyRead.model_validate(proxy)


@router.delete("/{proxy_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_proxy(
    proxy_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> Response:
    if not crud_proxy.delete_proxy(db, proxy_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Proxy not found"
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
