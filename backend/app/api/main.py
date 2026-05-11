"""FastAPI app, plugs all routers together."""

from __future__ import annotations

from fastapi import FastAPI

from app.api.routers import account, ai, media, orchestrator, proxy, task, user

app = FastAPI(
    title="Instagram CRM API",
    description=(
        "Sprint 1: core API and CRUD with subscription billing. "
        "Sprint 2: task orchestrator and Celery dispatch. "
        "Sprint 3: AI prompt to task plan. "
        "Sprint 5: media manager, ffmpeg uniqueization, account tagging."
    ),
    version="0.5.0",
)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


# routers
app.include_router(user.router)
app.include_router(proxy.router)
app.include_router(account.router)
app.include_router(task.router)
app.include_router(orchestrator.router)
app.include_router(ai.router)
app.include_router(media.router)
