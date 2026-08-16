"""One faculty member's month, end to end, against real PostgreSQL.

Everything is real except the mailbox: connect, browse, select, justify, submit,
bundle. The provider is a fake because a test that needs somebody's credentials
is a test that does not run.
"""

from __future__ import annotations

import datetime as _dt
import io
import json
import zipfile

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session
from tests.fakes.connector import PDF_REF, FakeConnector, header

from ccreport.accounts import connect_oauth_account, disconnect_account, open_secret
from ccreport.auth import Principal, authorize, bootstrap_allow_list
from ccreport.browse import browse
from ccreport.bundle import build_bundle
from ccreport.cache import HeaderCache
from ccreport.crypto import DevSecretBox
from ccreport.models import AuditLog, OAuthToken
from ccreport.oauth import TokenSet
from ccreport.reports import add_item, create_report, justify_item, submit_report
from ccreport.settings import Settings

FACULTY = "ada@princeton.edu"
NOW = _dt.datetime(2026, 8, 15, tzinfo=_dt.UTC)
PERIOD = "2026-07"
REFRESH = "rt-integration"  # noqa: S105 — a fixture


@pytest.fixture
def settings(tmp_path) -> Settings:
    import base64

    return Settings(
        environment="test",
        allowed_principals=FACULTY,
        session_secret="integration-secret",
        dev_encryption_key=base64.b64encode(b"0123456789abcdef0123456789abcdef").decode(),
        local_artifact_dir=str(tmp_path / "artifacts"),
        month_window=36,
    )


@pytest.fixture
def store(tmp_path):
    from ccreport.storage import LocalArtifactStore

    return LocalArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def connector() -> FakeConnector:
    return FakeConnector(
        [
            header("m1", "Amazon.com order receipt for $42.50", day=3, attachments=(PDF_REF,)),
            header("m2", "Faculty meeting Thursday", day=9, snippet="agenda"),
        ],
        attachments={("m1", "att-pdf"): b"%PDF-1.4 amazon\n"},
    )


def test_a_whole_month_from_sign_in_to_bundle(
    pg_session: Session, settings: Settings, store, connector: FakeConnector
) -> None:
    box = DevSecretBox.from_settings(settings)

    # Sign in: the allow-list is seeded, then the principal is authorized.
    bootstrap_allow_list(pg_session, settings)
    user = authorize(pg_session, Principal(upn=FACULTY, display_name="Ada"), settings)
    pg_session.flush()

    # Connect a mailbox: the credential is sealed on the way in.
    account = connect_oauth_account(
        pg_session,
        user,
        "graph",
        TokenSet(
            access_token="at",
            refresh_token=REFRESH,
            expires_at=NOW + _dt.timedelta(hours=1),
            scopes=("Mail.Read",),
            address=FACULTY,
        ),
        settings=settings,
        box=box,
    )
    pg_session.flush()
    stored = pg_session.get(OAuthToken, account.id)
    assert REFRESH.encode() not in stored.ciphertext
    assert open_secret(pg_session, account, box=box) == REFRESH

    # Browse: highlighted, and nothing written.
    result = browse(
        pg_session, user, account, PERIOD,
        connector=connector, settings=settings, receipts_only=True,
        cache=HeaderCache(ttl_seconds=600, max_entries=8), now=NOW,
    )
    assert [h.id for h in result.headers] == ["m1"]  # the meeting is not highlighted
    assert result.total == 2  # but it is still counted, never hidden

    # Select, justify, submit.
    report = create_report(pg_session, user, PERIOD, settings=settings, now=NOW)
    item = add_item(pg_session, user, report, account, result.headers[0])
    justify_item(pg_session, user, report, item.id, "Textbooks for ORF 405")
    submit_report(
        pg_session, user, report,
        open_connector=lambda _account: connector, store=store, settings=settings,
    )
    pg_session.flush()
    assert report.status == "submitted"

    # Bundle.
    archive = zipfile.ZipFile(io.BytesIO(build_bundle(report, user, store, generated_at=NOW)))
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["period"] == PERIOD
    assert manifest["items"][0]["justification"] == "Textbooks for ORF 405"
    assert archive.read("receipts/001-amazon.pdf") == b"%PDF-1.4 amazon\n"

    # The audit trail records what happened, in order.
    actions = [
        row.action
        for row in pg_session.query(AuditLog).order_by(AuditLog.at, AuditLog.action).all()
    ]
    assert "account.connect" in actions
    assert "report.submit" in actions


def test_browsing_opens_no_write_transaction_against_postgres(
    pg_session: Session, settings: Settings, connector: FakeConnector
) -> None:
    """The retention promise, checked against the database that will hold the data."""
    bootstrap_allow_list(pg_session, settings)
    user = authorize(pg_session, Principal(upn=FACULTY), settings)
    account = connect_oauth_account(
        pg_session, user, "graph",
        TokenSet(
            access_token="at", refresh_token=REFRESH,
            expires_at=NOW + _dt.timedelta(hours=1), address=FACULTY,
        ),
        settings=settings, box=DevSecretBox.from_settings(settings),
    )
    pg_session.flush()

    statements: list[str] = []

    @event.listens_for(pg_session.get_bind(), "before_cursor_execute")
    def record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement.strip().split()[0].upper())

    browse(
        pg_session, user, account, PERIOD,
        connector=connector, settings=settings,
        cache=HeaderCache(ttl_seconds=600, max_entries=8), now=NOW,
    )
    pg_session.flush()

    assert not [s for s in statements if s in {"INSERT", "UPDATE", "DELETE"}]


def test_disconnecting_removes_the_credential_from_the_database(
    pg_session: Session, settings: Settings
) -> None:
    bootstrap_allow_list(pg_session, settings)
    user = authorize(pg_session, Principal(upn=FACULTY), settings)
    account = connect_oauth_account(
        pg_session, user, "graph",
        TokenSet(
            access_token="at", refresh_token=REFRESH,
            expires_at=NOW + _dt.timedelta(hours=1), address=FACULTY,
        ),
        settings=settings, box=DevSecretBox.from_settings(settings),
    )
    pg_session.flush()
    account_id = account.id

    disconnect_account(pg_session, user, account_id)
    pg_session.flush()

    remaining = pg_session.execute(
        text("select count(*) from oauth_tokens where account_id = :id"), {"id": account_id}
    ).scalar()
    assert remaining == 0
