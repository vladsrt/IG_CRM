"""SQLAlchemy engine, session factory and the Base class."""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

# engine
engine = create_engine(
    settings.DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
)

# session factory
SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


# declarative base
class Base(DeclarativeBase):
    """Base class for all ORM models."""

    pass


# fastapi dependency
def get_db() -> Generator[Session, None, None]:
    """Give a db session and close it after the request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
