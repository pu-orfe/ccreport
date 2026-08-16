"""Regressions found in the v1 audit.

Each test here corresponds to a bug that shipped in a working-looking state: the
code ran, the tests passed, and the behaviour was wrong.
"""

from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tests.fakes.connector import FakeConnector
from tests.fakes.imap_server import FakeImap

from ccreport.accounts import connect_imap_account, open_connector, verify_account
from ccreport.errors import ConfigError
from ccreport.models import Report, User
from ccreport.reports import create_report, empty_report_summary
from ccreport.settings import Settings


def test_verifying_an_imap_account_does_not_forget_its_host(
    db_session: Session, faculty: User, settings: Settings, secret_box
) -> None:
    """`account test` used to overwrite the field the host was parsed out of.

    The connection then silently fell back to imap.gmail.com, so a mailbox on any
    other server stopped working the moment somebody tested it.
    """
    fake = FakeImap()
    account = connect_imap_account(
        db_session, faculty, "ada@example.edu", "app-password",
        host="mail.example.edu", port=1993,
        settings=settings, box=secret_box, verify=False,
    )

    verify_account(
        db_session, faculty, account, settings=settings, connector=FakeConnector(provider="imap")
    )

    assert (account.imap_host, account.imap_port) == ("mail.example.edu", 1993)
    connector = open_connector(
        db_session, account, settings=settings, box=secret_box, imap_factory=fake.factory()
    )
    assert (connector._host, connector._port) == ("mail.example.edu", 1993)


def test_a_person_cannot_hold_two_reports_for_one_month(
    db_session: Session, faculty: User, settings: Settings
) -> None:
    """`create_report` checks first, but two concurrent requests both find nothing."""
    create_report(db_session, faculty, "2026-07", settings=settings, now=_dt.datetime(2026, 8, 15, tzinfo=_dt.UTC))
    db_session.add(Report(user_id=faculty.id, period_year=2026, period_month=7, title="Duplicate"))

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_production_refuses_to_boot_without_a_session_secret() -> None:
    """Without it, OAuth state is unsigned and forms carry no CSRF token."""
    with pytest.raises(ConfigError, match="CCREPORT_SESSION_SECRET is required"):
        Settings(environment="production")


def test_azure_refuses_to_boot_without_a_session_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBSITE_SITE_NAME", "ccreport-prod")
    with pytest.raises(ConfigError, match="CCREPORT_SESSION_SECRET is required"):
        Settings(environment="test")


def test_development_still_runs_without_one() -> None:
    """The guard must not make a laptop unusable; locally it is only a warning."""
    assert Settings(environment="development").session_secret is None


def test_an_unstarted_month_renders_without_creating_anything() -> None:
    """Looking at a month is a read; a draft appears when a receipt is selected."""
    summary = empty_report_summary("2026-07")
    assert summary["id"] is None
    assert summary["items"] == 0
    assert summary["receipts"] == []
    assert summary["period_label"] == "July 2026"


def test_one_pooled_http_client_is_shared_rather_than_one_per_request() -> None:
    """A client per OAuth refresh leaked a connection pool on every browse."""
    from ccreport.httpclient import close_shared_client, shared_client

    first = shared_client()
    assert shared_client() is first
    close_shared_client()
    assert shared_client() is not first
    close_shared_client()


def test_a_connector_uses_the_shared_pool_by_default() -> None:
    from ccreport.connectors.graph import GraphConnector
    from ccreport.httpclient import close_shared_client, shared_client

    close_shared_client()
    connector = GraphConnector("token")
    assert connector._http is shared_client()
    close_shared_client()
