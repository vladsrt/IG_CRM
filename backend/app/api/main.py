"""fastapi app, plugs all routers together."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Configure logging BEFORE importing routers so their module-level loggers
# pick up the right handlers. Honours LOG_LEVEL / LOG_DIR env vars.
from app.core.logging_config import setup_logging

setup_logging(component="api")

from app.api.routers import (  # noqa: E402  (after setup_logging on purpose)
    account,
    admin,
    ai,
    auth,
    media,
    metrics,
    orchestrator,
    proxy,
    task,
    user,
)
from app.core.config import settings  # noqa: E402

app = FastAPI(
    title="Instagram CRM API",
    description=(
        "Sprint 1: core API and CRUD with subscription billing. "
        "Sprint 2: task orchestrator and Celery dispatch. "
        "Sprint 3: AI prompt to task plan. "
        "Sprint 5: media manager, ffmpeg uniqueization, account tagging. "
        "Sprint 6: JWT authentication and CORS. "
        "Sprint 7: background metrics collection and dashboard API."
    ),
    version="0.7.0",
)

# cors
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


# routers
app.include_router(auth.router)
app.include_router(user.router)
app.include_router(proxy.router)
app.include_router(account.router)
app.include_router(task.router)
app.include_router(orchestrator.router)
app.include_router(ai.router)
app.include_router(media.router)
app.include_router(metrics.router)
app.include_router(admin.router)

