"""Integration fixtures: the same code against a real PostgreSQL.

The unit suite proves the logic. This suite proves the things SQLite cannot:
that the migration produces exactly the schema the ORM expects, that the check
and unique constraints are really there, and that ``ON DELETE`` behaves.

The database URL is captured at import time because the root ``conftest``
deliberately strips every ``CCREPORT_`` variable from the environment before each
test.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

DATABASE_URL = os.environ.get("CCREPORT_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    "postgresql" not in DATABASE_URL,
    reason="integration tests need PostgreSQL; run `./ccreport test-integration`",
)


def _alembic_config(url: str):
    from pathlib import Path

    from alembic.config import Config

    package_root = Path(__file__).resolve().parents[2] / "src" / "ccreport"
    config = Config()
    config.set_main_option("script_location", str(package_root / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture(scope="session")
def database_url() -> str:
    """The URL captured at import time.

    A fixture rather than an import, because the root ``conftest`` strips
    ``CCREPORT_*`` from the environment before each test: anything that re-reads
    it later gets the default localhost URL and fails confusingly.
    """
    if "postgresql" not in DATABASE_URL:
        pytest.skip("no PostgreSQL configured")
    return DATABASE_URL


@pytest.fixture(scope="session")
def alembic_config(database_url: str):
    return _alembic_config(database_url)


@pytest.fixture(scope="session")
def migrated_engine() -> Iterator[Engine]:
    """A database built the way production is built: by Alembic, not create_all."""
    if "postgresql" not in DATABASE_URL:
        pytest.skip("no PostgreSQL configured")

    from alembic import command

    engine = create_engine(DATABASE_URL, future=True)
    with engine.begin() as connection:
        connection.execute(text("drop schema public cascade"))
        connection.execute(text("create schema public"))

    command.upgrade(_alembic_config(DATABASE_URL), "head")
    yield engine
    engine.dispose()


@pytest.fixture
def pg_session(migrated_engine: Engine) -> Iterator[Session]:
    """A session whose work is rolled back, so tests do not see each other."""
    connection = migrated_engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection, expire_on_commit=False, future=True)()
    try:
        yield session
    finally:
        session.close()
        # A test that provoked an IntegrityError has already lost its
        # transaction; rolling back again is what produces a confusing warning.
        if transaction.is_active:
            transaction.rollback()
        connection.close()
