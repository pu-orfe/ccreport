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


@pytest.mark.parametrize(
    "referer",
    [
        "/\\evil.example",          # browsers treat /\ as scheme-relative
        "//evil.example/steal",
        "https://evil.example/steal",
        "/nonsense/../../etc",
        "/reports/2026-07/../../accounts",
    ],
)
def test_no_referer_can_steer_a_redirect_off_our_own_pages(referer: str) -> None:
    """Every destination is a literal in `_return_to`, not filtered attacker text."""
    import re as _re

    from ccreport.web.app import _return_to

    destination = _return_to(_fake_request("/reports/2026-07/bundle.zip", referer))
    assert _re.fullmatch(r"/|/accounts|/admin|/reports/\d{4}-\d{2}", destination), destination
    assert "evil.example" not in destination


@pytest.mark.parametrize(
    ("referer", "expected"),
    [
        ("https://ccreport.example.edu/reports/2026-07", "/reports/2026-07"),
        ("https://ccreport.example.edu/accounts?msg=hi", "/accounts"),
        ("https://ccreport.example.edu/admin", "/admin"),
        ("", "/"),
    ],
)
def test_a_local_referer_returns_to_the_canonical_page(referer: str, expected: str) -> None:
    from ccreport.web.app import _return_to

    request = _fake_request("/reports/2026-07/bundle.zip", referer)
    assert _return_to(request) == expected


def _fake_request(path: str, referer: str):
    from starlette.datastructures import URL, Headers

    class _Request:
        url = URL(f"https://ccreport.example.edu{path}")
        headers = Headers({"referer": referer} if referer else {})

    return _Request()


def test_printed_output_can_never_carry_a_credential() -> None:
    """One command that forgets is one credential in somebody's scrollback."""
    from ccreport.cli import REDACTED, redact

    cleaned = redact(
        {
            "app_password": "hunter2",
            "refresh_token": "1//0erefresh",
            "client_secret": "shhh",
            "accounts": [{"password": "p", "address": "ada@princeton.edu"}],
        }
    )
    assert cleaned["app_password"] == REDACTED
    assert cleaned["refresh_token"] == REDACTED
    assert cleaned["client_secret"] == REDACTED
    assert cleaned["accounts"][0]["password"] == REDACTED
    assert cleaned["accounts"][0]["address"] == "ada@princeton.edu"
    assert "hunter2" not in repr(cleaned)


def test_redaction_matches_key_names_not_values() -> None:
    """`authorization_url` is the whole point of `account connect`; it must survive."""
    from ccreport.cli import redact

    url = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?code_challenge=abc"
    cleaned = redact({"authorization_url": url, "state": "signed-state", "provider": "graph"})
    assert cleaned["authorization_url"] == url
    assert cleaned["state"] == "signed-state"
