"""Lazy PostgreSQL engine and request-scoped SQLModel sessions."""

from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine
from sqlmodel import Session, create_engine

from app.core.config import get_database_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """Create one synchronous engine without connecting until first use."""
    return create_engine(str(get_database_settings().database_url), pool_pre_ping=True)


@lru_cache(maxsize=1)
def get_test_engine() -> Engine:
    """Engine for the isolated test database. Never points at production."""
    url = get_database_settings().test_database_url
    if url is None:
        raise RuntimeError("TEST_DATABASE_URL is not set")
    return create_engine(str(url), pool_pre_ping=True)


def get_session() -> Generator[Session, None, None]:
    """Yield a session and always close it; callers own transaction commits."""
    with Session(get_engine()) as session:
        yield session
