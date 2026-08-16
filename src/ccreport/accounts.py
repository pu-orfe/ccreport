"""Connected mailboxes: storing credentials, and turning them into connectors.

This is the only place a stored credential is decrypted, and the only place a
connector is constructed. Everything above it — the CLI, the web app, the report
builder — asks for a connector and never sees key material.

Access tokens are cached in memory for the life of the process and never
written to the database. Only the refresh token or app password is persisted,
sealed by :mod:`ccreport.crypto`, and only the *expiry* of the access token is
recorded, so the UI can say when a silent refresh is due.
"""

from __future__ import annotations

import datetime as _dt
import logging
import threading
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth.allowlist import audit
from .cache import get_header_cache
from .connectors.base import ConnectorStatus, MailConnector
from .connectors.gmail import GmailConnector
from .connectors.graph import GraphConnector
from .connectors.imap import ImapConnector
from .crypto import SealedSecret, SecretBox, get_secret_box
from .errors import ConfigError, ConnectorError, NotAuthorized, ReauthRequired
from .models import MailAccount, OAuthToken, User
from .oauth import TokenSet, build_oauth_client
from .settings import Settings, get_settings

logger = logging.getLogger("ccreport.accounts")

#: Refresh a little before expiry. A token that expires mid-request is a failure
#: the user sees; a token refreshed 60 seconds early is one they never do.
ACCESS_TOKEN_SKEW_SECONDS = 60

STATUS_CONNECTED = "connected"
STATUS_NEEDS_REAUTH = "needs_reauth"
STATUS_REVOKED = "revoked"


class AccountNotFound(NotAuthorized):
    """No such connection, or it does not belong to the caller.

    Deliberately the same exception either way: telling a caller that an account
    exists but is somebody else's is itself a disclosure.
    """


@dataclass(slots=True)
class _CachedToken:
    access_token: str
    expires_at: _dt.datetime


class _AccessTokenCache:
    """Process-local access tokens. Never persisted, never shared."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tokens: dict[str, _CachedToken] = {}

    def get(self, account_id: str) -> str | None:
        with self._lock:
            entry = self._tokens.get(account_id)
            if entry is None:
                return None
            deadline = _dt.datetime.now(_dt.UTC) + _dt.timedelta(seconds=ACCESS_TOKEN_SKEW_SECONDS)
            if entry.expires_at <= deadline:
                del self._tokens[account_id]
                return None
            return entry.access_token

    def put(self, account_id: str, token: str, expires_at: _dt.datetime) -> None:
        with self._lock:
            self._tokens[account_id] = _CachedToken(token, expires_at)

    def drop(self, account_id: str) -> None:
        with self._lock:
            self._tokens.pop(account_id, None)

    def clear(self) -> None:
        with self._lock:
            self._tokens.clear()


_access_tokens = _AccessTokenCache()


def clear_access_tokens() -> None:
    """Drop every cached access token. For tests and for a settings reload."""
    _access_tokens.clear()


# --------------------------------------------------------------- credentials
def seal_secret(
    session: Session, account: MailAccount, secret: str, *, box: SecretBox | None = None
) -> OAuthToken:
    """Store the long-lived credential for ``account``, replacing any previous one."""
    box = box or get_secret_box()
    sealed = box.seal(secret.encode("utf-8"))
    token = session.get(OAuthToken, account.id)
    if token is None:
        token = OAuthToken(account_id=account.id, wrapped_dek=b"", key_name="", nonce=b"", ciphertext=b"")
        session.add(token)
    token.wrapped_dek = sealed.wrapped_dek
    token.key_name = sealed.key_name
    token.key_version = sealed.key_version
    token.nonce = sealed.nonce
    token.ciphertext = sealed.ciphertext
    token.refreshed_at = _dt.datetime.now(_dt.UTC)
    session.flush()
    return token


def open_secret(session: Session, account: MailAccount, *, box: SecretBox | None = None) -> str:
    box = box or get_secret_box()
    token = session.get(OAuthToken, account.id)
    if token is None:
        raise ReauthRequired(
            f"no stored credential for {account.address}; reconnect this account."
        )
    sealed = SealedSecret(
        wrapped_dek=token.wrapped_dek,
        key_name=token.key_name,
        key_version=token.key_version,
        nonce=token.nonce,
        ciphertext=token.ciphertext,
    )
    return box.open(sealed).decode("utf-8")


# ------------------------------------------------------------------ connect
def _guard_personal_gmail(address: str, settings: Settings) -> None:
    """Personal Gmail over OAuth stays off unless verification is declared.

    ``gmail.readonly`` is a restricted scope. An unverified client caps at 100
    users and expires refresh tokens weekly, so allowing personal addresses
    through by accident produces an application that appears to work and then
    logs everybody out every seven days.
    """
    domain = address.rsplit("@", 1)[-1].lower() if "@" in address else ""
    hosted = (settings.google_hosted_domain or "").lower()
    if hosted and domain == hosted:
        return
    if not settings.enable_personal_gmail_oauth:
        raise ConfigError(
            f"{address} is not a {hosted or 'Workspace'} address. Personal Gmail "
            "over OAuth needs a fully verified client and an annual CASA "
            "assessment; connect it over IMAP with an app password instead."
        )


def connect_oauth_account(
    session: Session,
    user: User,
    provider: str,
    tokens: TokenSet,
    *,
    settings: Settings | None = None,
    box: SecretBox | None = None,
    address: str | None = None,
) -> MailAccount:
    """Record a completed OAuth authorization as a connected mailbox."""
    settings = settings or get_settings()
    address = (address or tokens.address or "").strip().lower()
    if not address:
        raise ConnectorError(
            f"the {provider} authorization did not identify a mailbox address; "
            "the connection cannot be labelled and was not saved."
        )
    if not tokens.refresh_token:
        raise ConnectorError(
            f"the {provider} authorization returned no refresh token, so the "
            "connection would stop working within the hour. It was not saved."
        )
    if provider == "gmail":
        _guard_personal_gmail(address, settings)

    account = session.scalar(
        select(MailAccount).where(
            MailAccount.user_id == user.id,
            MailAccount.provider == provider,
            MailAccount.address == address,
        )
    )
    if account is None:
        account = MailAccount(user_id=user.id, provider=provider, address=address)
        session.add(account)
        session.flush()

    account.status = STATUS_CONNECTED
    account.status_detail = None
    account.granted_scopes = tokens.scope_string or None
    account.posture = settings.google_oauth_publishing_status if provider == "gmail" else None
    account.connected_at = _dt.datetime.now(_dt.UTC)
    account.revoked_at = None
    account.display_name = tokens.claims.get("name") or account.display_name

    seal_secret(session, account, tokens.refresh_token, box=box)
    session.get(OAuthToken, account.id).access_expires_at = tokens.expires_at
    _access_tokens.put(account.id, tokens.access_token, tokens.expires_at)

    audit(
        session,
        actor=user.upn,
        action="account.connect",
        subject_type="mail_account",
        subject_id=account.id,
        detail={"provider": provider, "address": address, "scopes": tokens.scope_string},
    )
    return account


def connect_imap_account(
    session: Session,
    user: User,
    address: str,
    app_password: str,
    *,
    host: str | None = None,
    port: int | None = None,
    settings: Settings | None = None,
    box: SecretBox | None = None,
    verify: bool = True,
    imap_factory=None,
) -> MailAccount:
    """Connect a mailbox with an IMAP app password.

    Verified before it is stored: an app password with a typo would otherwise
    be discovered on the first browse, several minutes and one confused user later.
    """
    settings = settings or get_settings()
    if not settings.enable_imap:
        raise ConfigError("the IMAP connector is disabled (CCREPORT_ENABLE_IMAP=false)")
    address = address.strip().lower()
    host = host or settings.imap_default_host
    port = int(port or settings.imap_default_port)

    if verify:
        probe = ImapConnector(host, port, address, app_password, imap_factory=imap_factory)
        status = probe.status()
        if not status.ok:
            raise ConnectorError(f"IMAP rejected the credential: {status.detail}")

    account = session.scalar(
        select(MailAccount).where(
            MailAccount.user_id == user.id,
            MailAccount.provider == "imap",
            MailAccount.address == address,
        )
    )
    if account is None:
        account = MailAccount(user_id=user.id, provider="imap", address=address)
        session.add(account)
        session.flush()

    account.status = STATUS_CONNECTED
    account.status_detail = None
    account.imap_host = host
    account.imap_port = port
    account.granted_scopes = "imap:examine"
    account.connected_at = _dt.datetime.now(_dt.UTC)
    account.last_verified_at = _dt.datetime.now(_dt.UTC) if verify else None
    account.revoked_at = None

    seal_secret(session, account, app_password, box=box)
    audit(
        session,
        actor=user.upn,
        action="account.connect",
        subject_type="mail_account",
        subject_id=account.id,
        detail={"provider": "imap", "address": address, "host": host, "port": port},
    )
    return account


# ------------------------------------------------------------------- lookup
def list_accounts(session: Session, user: User, *, include_revoked: bool = False) -> list[MailAccount]:
    stmt = select(MailAccount).where(MailAccount.user_id == user.id)
    if not include_revoked:
        stmt = stmt.where(MailAccount.status != STATUS_REVOKED)
    return list(session.scalars(stmt.order_by(MailAccount.provider, MailAccount.address)).all())


def get_account(session: Session, user: User, account_id: str) -> MailAccount:
    account = session.get(MailAccount, account_id)
    if account is None or account.user_id != user.id:
        raise AccountNotFound(f"no connected mailbox with id {account_id!r}")
    return account


def resolve_account(session: Session, user: User, reference: str) -> MailAccount:
    """Find a mailbox by id, by address, or by provider when it is unambiguous.

    Typing a UUID is not a user interface. ``--account outlook`` and
    ``--account ada@princeton.edu`` are, and both appear in the README.
    """
    reference = reference.strip().lower()
    accounts = list_accounts(session, user)
    by_id = {a.id: a for a in accounts}
    if reference in by_id:
        return by_id[reference]

    aliases = {"outlook": "graph", "microsoft": "graph", "graph": "graph", "gmail": "gmail",
               "google": "gmail", "imap": "imap"}
    matches = [a for a in accounts if a.address == reference]
    if not matches and reference in aliases:
        matches = [a for a in accounts if a.provider == aliases[reference]]
    if not matches:
        matches = [a for a in accounts if a.address.startswith(reference)]

    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise AccountNotFound(f"no connected mailbox matches {reference!r}")
    listed = ", ".join(f"{a.address} ({a.id})" for a in matches)
    raise AccountNotFound(f"{reference!r} matches more than one mailbox: {listed}")


# ------------------------------------------------------------------ lifecycle
def disconnect_account(
    session: Session, user: User, account_id: str, *, forget: bool = True
) -> MailAccount:
    """Revoke and forget a connection.

    The stored credential is deleted rather than marked inactive. A revoked
    account whose refresh token is still in the database is still a refresh
    token in the database.
    """
    account = get_account(session, user, account_id)
    token = session.get(OAuthToken, account.id)
    if token is not None:
        session.delete(token)
    _access_tokens.drop(account.id)
    get_header_cache().invalidate_account(user.id, account.id)

    account.status = STATUS_REVOKED
    account.status_detail = "disconnected by the account owner"
    account.revoked_at = _dt.datetime.now(_dt.UTC)
    session.flush()

    if forget:
        # Items in submitted reports keep working: source_account_id is
        # ON DELETE SET NULL precisely so history survives a disconnect.
        session.delete(account)

    audit(
        session,
        actor=user.upn,
        action="account.disconnect",
        subject_type="mail_account",
        subject_id=account_id,
        detail={"provider": account.provider, "address": account.address, "forgotten": forget},
    )
    return account


def verify_account(
    session: Session,
    user: User,
    account: MailAccount,
    *,
    settings: Settings | None = None,
    box: SecretBox | None = None,
    connector: MailConnector | None = None,
) -> ConnectorStatus:
    """Verify one credential and record what happened."""
    settings = settings or get_settings()
    try:
        connector = connector or open_connector(session, account, settings=settings, box=box)
        status = connector.status()
    except ReauthRequired as exc:
        account.status = STATUS_NEEDS_REAUTH
        account.status_detail = str(exc)
        session.flush()
        return ConnectorStatus(
            ok=False, provider=account.provider, address=account.address,
            detail=str(exc), needs_reauth=True,
        )
    except (ConnectorError, ConfigError) as exc:
        account.status_detail = str(exc)
        session.flush()
        return ConnectorStatus(
            ok=False, provider=account.provider, address=account.address, detail=str(exc)
        )

    warnings = list(status.warnings)
    if account.provider == "gmail" and not settings.google_posture_is_durable:
        warnings.append(
            "The Google OAuth client is in "
            f"'{settings.google_oauth_publishing_status}' posture, so refresh "
            "tokens expire after seven days. See docs/OIT-REQUESTS.md."
        )
    account.status = STATUS_CONNECTED
    account.status_detail = "; ".join(warnings) or None
    account.last_verified_at = _dt.datetime.now(_dt.UTC)
    session.flush()
    return ConnectorStatus(
        ok=True,
        provider=account.provider,
        address=status.address or account.address,
        detail=status.detail,
        warnings=tuple(warnings),
    )


# ----------------------------------------------------------------- connectors
def _access_token_for(
    session: Session, account: MailAccount, settings: Settings, box: SecretBox | None
) -> str:
    cached = _access_tokens.get(account.id)
    if cached:
        return cached

    refresh_token = open_secret(session, account, box=box)
    client = build_oauth_client(account.provider, settings)
    try:
        tokens = client.refresh(refresh_token)
    except ReauthRequired:
        account.status = STATUS_NEEDS_REAUTH
        account.status_detail = "the provider rejected the stored credential"
        session.flush()
        raise

    if tokens.refresh_token and tokens.refresh_token != refresh_token:
        # Microsoft rotates refresh tokens; losing the new one costs the user a
        # reconnect at some unpredictable point in the future.
        seal_secret(session, account, tokens.refresh_token, box=box)
    stored = session.get(OAuthToken, account.id)
    if stored is not None:
        stored.access_expires_at = tokens.expires_at
        stored.refreshed_at = _dt.datetime.now(_dt.UTC)
    account.status = STATUS_CONNECTED
    session.flush()

    _access_tokens.put(account.id, tokens.access_token, tokens.expires_at)
    return tokens.access_token


def open_connector(
    session: Session,
    account: MailAccount,
    *,
    settings: Settings | None = None,
    box: SecretBox | None = None,
    imap_factory=None,
) -> MailConnector:
    """Build a read-only connector for one stored account."""
    settings = settings or get_settings()
    if account.status == STATUS_REVOKED:
        raise ReauthRequired(f"{account.address} was disconnected; reconnect it to browse again.")

    if account.provider == "imap":
        return ImapConnector(
            account.imap_host or settings.imap_default_host,
            account.imap_port or settings.imap_default_port,
            account.address,
            open_secret(session, account, box=box),
            imap_factory=imap_factory,
        )

    access_token = _access_token_for(session, account, settings, box)
    if account.provider == "graph":
        return GraphConnector(access_token, address=account.address)
    if account.provider == "gmail":
        return GmailConnector(
            access_token,
            address=account.address,
            posture=settings.google_oauth_publishing_status,
        )
    raise ConfigError(f"unknown mail provider {account.provider!r}")


def account_summary(account: MailAccount) -> dict:
    """The shape the CLI prints and the web app renders."""
    return {
        "id": account.id,
        "provider": account.provider,
        "address": account.address,
        "display_name": account.display_name,
        "status": account.status,
        "status_detail": account.status_detail,
        "scopes": account.granted_scopes,
        "posture": account.posture,
        "host": f"{account.imap_host}:{account.imap_port}" if account.imap_host else None,
        "connected_at": account.connected_at.isoformat() if account.connected_at else None,
        "last_verified_at": (
            account.last_verified_at.isoformat() if account.last_verified_at else None
        ),
    }


__all__ = [
    "STATUS_CONNECTED",
    "STATUS_NEEDS_REAUTH",
    "STATUS_REVOKED",
    "AccountNotFound",
    "account_summary",
    "clear_access_tokens",
    "connect_imap_account",
    "connect_oauth_account",
    "disconnect_account",
    "get_account",
    "list_accounts",
    "open_connector",
    "open_secret",
    "resolve_account",
    "seal_secret",
    "verify_account",
]
