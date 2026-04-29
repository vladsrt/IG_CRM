"""SQLAlchemy engine, session factory, and declarative Base."""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

#DB
# ── Engine ──────────────────────────────────────────────────────────────
engine = create_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
)

# ── Session factory ─────────────────────────────────────────────────────
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


# ── Declarative Base ────────────────────────────────────────────────────
class Base(DeclarativeBase):
    """Base class that all ORM models inherit from."""

    pass


# ── FastAPI dependency ──────────────────────────────────────────────────
def get_db() -> Generator[Session, None, None]:
    """Yield a database session and ensure it is closed after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
