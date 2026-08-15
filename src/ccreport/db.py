"""Database engine and session management.

Deliberately thin. The engine is created once, lazily, so importing this module
in a test that never touches a database costs nothing and requires no
``DATABASE_URL``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base
from .settings import Settings, get_settings


@lru_cache(maxsize=4)
def _engine_for(url: str, echo: bool) -> Engine:
    kwargs: dict = {"echo": echo, "future": True}
    if url.startswith("sqlite"):
        # SQLite is only used by tests that do not need concurrency; the
        # deployed application runs on PostgreSQL.
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        # A B1ms Postgres has a modest connection budget and App Service can
        # hold several workers. Recycle before the server does it for us.
        kwargs.update(pool_pre_ping=True, pool_size=5, max_overflow=5, pool_recycle=1800)
    return create_engine(url, **kwargs)


def get_engine(settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    return _engine_for(settings.database_url, settings.log_level.upper() == "DEBUG")


def get_sessionmaker(settings: Settings | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(settings), expire_on_commit=False, future=True)


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    """A transactional scope. Commits on success, rolls back on anything else."""
    factory = get_sessionmaker(settings)
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all(settings: Settings | None = None) -> None:
    """Create every table directly.

    For tests and first-run local development only. Deployed environments run
    Alembic, so that a schema change is a reviewable migration rather than a
    surprise on restart.
    """
    Base.metadata.create_all(get_engine(settings))


__all__ = ["create_all", "get_engine", "get_sessionmaker", "session_scope"]
