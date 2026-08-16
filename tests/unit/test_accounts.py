from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy.orm import Session
from tests.fakes.connector import FakeConnector
from tests.fakes.imap_server import FakeImap

from ccreport.accounts import (
    AccountNotFound,
    account_summary,
    connect_imap_account,
    connect_oauth_account,
    disconnect_account,
    list_accounts,
    open_connector,
    open_secret,
    resolve_account,
    verify_account,
)
from ccreport.errors import ConfigError, ConnectorError
from ccreport.models import MailAccount, OAuthToken, User
from ccreport.oauth import TokenSet
from ccreport.settings import Settings

REFRESH = "rt-institutional"  # noqa: S105 — a fixture


def tokens(address: str = "ada@princeton.edu", refresh: str | None = REFRESH) -> TokenSet:
    return TokenSet(
        access_token="at-1",
        refresh_token=refresh,
        expires_at=_dt.datetime.now(_dt.UTC) + _dt.timedelta(hours=1),
        scopes=("Mail.Read", "offline_access"),
        address=address,
        claims={"name": "Ada Lovelace"},
    )


# ------------------------------------------------------------------ connecting
def test_connecting_stores_only_ciphertext(
    db_session: Session, faculty: User, settings: Settings, secret_box
) -> None:
    account = connect_oauth_account(
        db_session, faculty, "graph", tokens(), settings=settings, box=secret_box
    )
    stored = db_session.get(OAuthToken, account.id)

    assert REFRESH.encode() not in stored.ciphertext
    assert REFRESH.encode() not in stored.wrapped_dek
    assert open_secret(db_session, account, box=secret_box) == REFRESH
    assert account.status == "connected"
    assert account.granted_scopes == "Mail.Read offline_access"


def test_reconnecting_the_same_mailbox_replaces_rather_than_duplicates(
    db_session: Session, faculty: User, settings: Settings, secret_box
) -> None:
    first = connect_oauth_account(db_session, faculty, "graph", tokens(), settings=settings, box=secret_box)
    second = connect_oauth_account(
        db_session, faculty, "graph", tokens(refresh="rt-2"), settings=settings, box=secret_box
    )
    assert first.id == second.id
    assert len(list_accounts(db_session, faculty)) == 1
    assert open_secret(db_session, second, box=secret_box) == "rt-2"


def test_an_authorization_without_a_refresh_token_is_refused(
    db_session: Session, faculty: User, settings: Settings, secret_box
) -> None:
    """A connection that dies within the hour is worse than no connection."""
    with pytest.raises(ConnectorError, match="no refresh token"):
        connect_oauth_account(
            db_session, faculty, "graph", tokens(refresh=None), settings=settings, box=secret_box
        )
    assert list_accounts(db_session, faculty) == []


def test_an_authorization_without_an_address_is_refused(
    db_session: Session, faculty: User, settings: Settings, secret_box
) -> None:
    with pytest.raises(ConnectorError, match="did not identify a mailbox"):
        connect_oauth_account(
            db_session, faculty, "graph", tokens(address=""), settings=settings, box=secret_box
        )


def test_personal_gmail_over_oauth_is_refused_unless_declared_verified(
    db_session: Session, faculty: User, settings: Settings, secret_box
) -> None:
    with pytest.raises(ConfigError, match="IMAP with an app password|app password"):
        connect_oauth_account(
            db_session, faculty, "gmail", tokens(address="ada@gmail.com"),
            settings=settings, box=secret_box,
        )


def test_workspace_gmail_connects_and_records_the_consent_posture(
    db_session: Session, faculty: User, settings: Settings, secret_box
) -> None:
    account = connect_oauth_account(
        db_session, faculty, "gmail", tokens(address="ada@princeton.edu"),
        settings=settings, box=secret_box,
    )
    assert account.posture == "internal"


def test_imap_connects_with_an_app_password_after_verifying_it(
    db_session: Session, faculty: User, settings: Settings, secret_box
) -> None:
    fake = FakeImap()
    account = connect_imap_account(
        db_session, faculty, "ada@gmail.com", "app-password",
        settings=settings, box=secret_box, imap_factory=fake.factory(),
    )
    assert account.provider == "imap"
    assert (account.imap_host, account.imap_port) == ("imap.gmail.com", 993)
    assert account.last_verified_at is not None
    assert open_secret(db_session, account, box=secret_box) == "app-password"


def test_imap_can_be_disabled_for_a_deployment(
    db_session: Session, faculty: User, settings: Settings, secret_box
) -> None:
    disabled = settings.model_copy(update={"enable_imap": False})
    with pytest.raises(ConfigError, match="disabled"):
        connect_imap_account(
            db_session, faculty, "ada@gmail.com", "pw", settings=disabled, box=secret_box, verify=False
        )


# -------------------------------------------------------------------- lookup
def test_accounts_resolve_by_id_address_or_provider_alias(
    db_session: Session, faculty: User, settings: Settings, secret_box
) -> None:
    account = connect_oauth_account(db_session, faculty, "graph", tokens(), settings=settings, box=secret_box)

    assert resolve_account(db_session, faculty, account.id).id == account.id
    assert resolve_account(db_session, faculty, "ada@princeton.edu").id == account.id
    assert resolve_account(db_session, faculty, "outlook").id == account.id
    assert resolve_account(db_session, faculty, "GRAPH").id == account.id


def test_another_users_account_is_indistinguishable_from_one_that_does_not_exist(
    db_session: Session, faculty: User, settings: Settings, secret_box
) -> None:
    stranger = User(upn="bob@princeton.edu", role="faculty")
    db_session.add(stranger)
    db_session.flush()
    theirs = connect_oauth_account(
        db_session, stranger, "graph", tokens(address="bob@princeton.edu"),
        settings=settings, box=secret_box,
    )
    with pytest.raises(AccountNotFound):
        resolve_account(db_session, faculty, theirs.id)


def test_ambiguous_references_name_the_candidates(
    db_session: Session, faculty: User, settings: Settings, secret_box
) -> None:
    connect_oauth_account(db_session, faculty, "graph", tokens(), settings=settings, box=secret_box)
    connect_oauth_account(
        db_session, faculty, "gmail", tokens(address="ada2@princeton.edu"),
        settings=settings, box=secret_box,
    )
    with pytest.raises(AccountNotFound, match="more than one mailbox"):
        resolve_account(db_session, faculty, "ada")


# --------------------------------------------------------------- disconnecting
def test_disconnecting_deletes_the_credential(
    db_session: Session, faculty: User, settings: Settings, secret_box
) -> None:
    account = connect_oauth_account(db_session, faculty, "graph", tokens(), settings=settings, box=secret_box)
    account_id = account.id

    disconnect_account(db_session, faculty, account_id)

    assert db_session.get(OAuthToken, account_id) is None
    assert db_session.get(MailAccount, account_id) is None
    assert list_accounts(db_session, faculty) == []


def test_disconnecting_drops_the_cached_browse_results(
    db_session: Session, faculty: User, settings: Settings, secret_box
) -> None:
    from tests.fakes.connector import header

    from ccreport.cache import CacheKey, get_header_cache

    account = connect_oauth_account(db_session, faculty, "graph", tokens(), settings=settings, box=secret_box)
    cache = get_header_cache(settings)
    key = CacheKey(user_id=faculty.id, account_id=account.id, period="2026-07")
    cache.put(key, [header("m1")])

    disconnect_account(db_session, faculty, account.id)
    assert cache.get(key) is None


# ---------------------------------------------------------------------- test
def test_verifying_an_account_records_posture_warnings(
    db_session: Session, faculty: User, settings: Settings, secret_box
) -> None:
    testing_posture = settings.model_copy(update={"google_oauth_publishing_status": "testing"})
    account = connect_oauth_account(
        db_session, faculty, "gmail", tokens(), settings=testing_posture, box=secret_box
    )
    status = verify_account(
        db_session, faculty, account, settings=testing_posture, connector=FakeConnector(provider="gmail")
    )
    assert status.ok
    assert any("seven days" in w for w in status.warnings)
    assert account.last_verified_at is not None


def test_a_dead_credential_marks_the_account_for_reconnection(
    db_session: Session, faculty: User, settings: Settings, secret_box
) -> None:
    from ccreport.errors import ReauthRequired

    class DeadConnector(FakeConnector):
        def status(self):
            raise ReauthRequired("the provider rejected the stored credential")

    account = connect_oauth_account(db_session, faculty, "graph", tokens(), settings=settings, box=secret_box)
    status = verify_account(db_session, faculty, account, settings=settings, connector=DeadConnector())

    assert not status.ok and status.needs_reauth
    assert account.status == "needs_reauth"


# ----------------------------------------------------------------- connectors
def test_opening_an_imap_connector_uses_the_stored_host_and_password(
    db_session: Session, faculty: User, settings: Settings, secret_box
) -> None:
    fake = FakeImap()
    account = connect_imap_account(
        db_session, faculty, "ada@gmail.com", "app-password", host="imap.example", port=1993,
        settings=settings, box=secret_box, verify=False,
    )
    connector = open_connector(
        db_session, account, settings=settings, box=secret_box, imap_factory=fake.factory()
    )
    assert connector.provider == "imap"
    assert connector.status().address == "ada@gmail.com"


def test_summary_never_includes_credential_material(
    db_session: Session, faculty: User, settings: Settings, secret_box
) -> None:
    account = connect_oauth_account(db_session, faculty, "graph", tokens(), settings=settings, box=secret_box)
    summary = account_summary(account)
    assert REFRESH not in repr(summary)
    assert set(summary) == {
        "id", "provider", "address", "display_name", "status", "status_detail",
        "scopes", "posture", "host", "connected_at", "last_verified_at",
    }
