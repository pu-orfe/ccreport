"""Shared pytest fixtures.

The unit suite runs with no network, no credentials and no PostgreSQL: an
in-memory SQLite database is enough to exercise the schema's shape and every
authorization path. The integration suite runs the same models against real
PostgreSQL in Compose, which is where migrations and dialect differences get
caught.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ccreport.accounts import clear_access_tokens
from ccreport.auth import bootstrap_allow_list
from ccreport.cache import reset_header_cache
from ccreport.crypto import DevSecretBox
from ccreport.models import Base, User
from ccreport.settings import Settings, get_settings
from ccreport.storage import LocalArtifactStore

FACULTY_UPN = "ada@princeton.edu"
ADMIN_UPN = "grace@princeton.edu"
#: A fixed key so a failing test prints something reproducible.
TEST_DEK = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the developer's own .env and Azure markers out of the tests."""
    for key in list(os.environ):
        if key.startswith("CCREPORT_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("WEBSITE_SITE_NAME", raising=False)
    get_settings.cache_clear()
    reset_header_cache()
    clear_access_tokens()
    yield
    get_settings.cache_clear()
    reset_header_cache()
    clear_access_tokens()


@pytest.fixture
def db_session() -> Iterator[Session]:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture
def settings(tmp_path) -> Settings:
    """A configured, non-production deployment that touches nothing outside tmp_path."""
    return Settings(
        environment="test",
        allowed_principals=f"{FACULTY_UPN},{ADMIN_UPN}",
        admin_principals=ADMIN_UPN,
        session_secret="test-only-session-secret",
        dev_encryption_key=TEST_DEK,
        local_artifact_dir=str(tmp_path / "artifacts"),
        database_url="sqlite://",
        base_url="https://ccreport.example.edu",
        ms_client_id="ms-client",
        ms_client_secret="ms-secret",
        google_client_id="google-client",
        google_client_secret="google-secret",
        google_oauth_publishing_status="internal",
    )


@pytest.fixture
def secret_box(settings: Settings) -> DevSecretBox:
    return DevSecretBox.from_settings(settings)


@pytest.fixture
def store(tmp_path) -> LocalArtifactStore:
    return LocalArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def pdf_renderer() -> None:
    """Skip when no PDF renderer works here.

    WeasyPrint imports but raises ``OSError`` when Cairo and Pango are missing,
    which is the normal state of a laptop that has not run ``brew install pango``.
    The container the application ships in always has them, so these tests run in
    ``test-docker`` and in CI.
    """
    try:
        import weasyprint  # noqa: F401
    except Exception as exc:  # ImportError, OSError from cffi.dlopen
        pytest.skip(f"no usable PDF renderer: {exc}")


@pytest.fixture
def faculty(db_session: Session, settings: Settings) -> User:
    bootstrap_allow_list(db_session, settings)
    user = User(upn=FACULTY_UPN, display_name="Ada Lovelace", role="faculty")
    db_session.add(user)
    db_session.flush()
    return user


@pytest.fixture
def admin(db_session: Session, settings: Settings) -> User:
    bootstrap_allow_list(db_session, settings)
    user = User(upn=ADMIN_UPN, display_name="Grace Hopper", role="admin")
    db_session.add(user)
    db_session.flush()
    return user
