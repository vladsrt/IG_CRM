"""FastAPI application entrypoint — aggregates all routers."""

from __future__ import annotations

from fastapi import FastAPI

from app.api.routers import account, ai, orchestrator, proxy, task, user

app = FastAPI(
    title="Instagram CRM API",
    description=(
        "Sprint 1 — Core API & CRUD (Subscription SaaS billing model). "
        "Sprint 2 — Task Orchestrator & Celery dispatch. "
        "Sprint 3 — AI prompt → Task plan."
    ),
    version="0.3.0",
)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


# ── Router aggregation ──────────────────────────────────────────────────
app.include_router(user.router)
app.include_router(proxy.router)
app.include_router(account.router)
app.include_router(task.router)
app.include_router(orchestrator.router)
app.include_router(ai.router)
