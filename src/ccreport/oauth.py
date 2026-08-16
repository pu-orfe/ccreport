"""Mailbox authorization: the second authentication plane.

Signing in to ccreport says who you are. It says nothing about which mailboxes
ccreport may read, and this module is the whole of the mechanism that answers
that second question. Connecting a mailbox is an explicit act by the mailbox
owner, against a client that asks for read scopes only, and it is revocable from
the provider's own account page without involving us at all.

Three details are load-bearing:

* **PKCE on every flow.** Both clients are confidential and could rely on the
  secret alone; the code challenge costs nothing and removes an entire class of
  interception attack from a redirect chain that passes through a browser.
* **Signed, expiring state.** ``state`` carries the user, the provider and the
  PKCE verifier in a token signed with the session secret and valid for ten
  minutes. An unsigned state parameter is a CSRF hole with extra steps.
* **The ID token is read, not trusted.** It arrives over TLS directly from the
  token endpoint in exchange for a code we generated, so it is used only to
  label the connection with an address. No access decision is made from it —
  those come from the allow-list.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from typing import Any

import httpx

from .errors import ConfigError, ConnectorError, ReauthRequired
from .httpclient import shared_client
from .settings import Settings, get_settings

#: How long an authorization round-trip may take. Long enough for a consent
#: screen and a password manager, short enough that a leaked URL goes stale.
STATE_MAX_AGE_SECONDS = 600
_STATE_SALT = "ccreport-oauth-state"

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 — a URL, not a secret
GOOGLE_REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def new_code_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("ascii").rstrip("=")


def code_challenge_for(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def decode_id_token_claims(id_token: str | None) -> dict[str, Any]:
    """Read the payload of an ID token without verifying its signature.

    Safe here and nowhere else: this token came back over TLS from the token
    endpoint, in direct exchange for a code this process generated. It is used
    to label a connection with an email address, never to authorize anything.
    """
    if not id_token or id_token.count(".") != 2:
        return {}
    try:
        payload = json.loads(_b64url_decode(id_token.split(".")[1]))
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


@dataclass(frozen=True, slots=True)
class TokenSet:
    """What a token endpoint gave back, normalised."""

    access_token: str
    refresh_token: str | None
    expires_at: _dt.datetime
    scopes: tuple[str, ...] = ()
    address: str | None = None
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def scope_string(self) -> str:
        return " ".join(self.scopes)

    def expires_in(self, now: _dt.datetime | None = None) -> float:
        now = now or _dt.datetime.now(_dt.UTC)
        return (self.expires_at - now).total_seconds()


class StateSigner:
    """Signs the OAuth ``state`` parameter and everything travelling with it."""

    def __init__(self, secret: str, *, salt: str = _STATE_SALT):
        if not secret:
            raise ConfigError(
                "CCREPORT_SESSION_SECRET is required to start a mailbox "
                "authorization; without it the OAuth state parameter cannot be signed."
            )
        from itsdangerous import URLSafeTimedSerializer

        self._serializer = URLSafeTimedSerializer(secret, salt=salt)

    @classmethod
    def from_settings(cls, settings: Settings | None = None, *, salt: str = _STATE_SALT) -> StateSigner:
        """``salt`` separates purposes: a form token must not pass as OAuth state."""
        settings = settings or get_settings()
        secret = settings.session_secret.get_secret_value() if settings.session_secret else ""
        return cls(secret, salt=salt)

    def sign(self, payload: dict[str, Any]) -> str:
        return self._serializer.dumps({**payload, "nonce": secrets.token_urlsafe(8)})

    def unsign(self, token: str, *, max_age: int = STATE_MAX_AGE_SECONDS) -> dict[str, Any]:
        from itsdangerous import BadSignature, SignatureExpired

        try:
            data = self._serializer.loads(token, max_age=max_age)
        except SignatureExpired as exc:
            raise ConnectorError(
                "this mailbox authorization took too long and has expired; start it again"
            ) from exc
        except BadSignature as exc:
            raise ConnectorError(
                "the authorization response did not carry a state value we issued; "
                "refusing to complete it"
            ) from exc
        if not isinstance(data, dict):
            raise ConnectorError("malformed authorization state")
        return data


class _OAuthClient:
    """Shared token-endpoint mechanics for both providers."""

    provider = ""
    token_endpoint = ""

    def __init__(self, client_id: str, client_secret: str, *, http: httpx.Client | None = None):
        if not client_id or not client_secret:
            raise ConfigError(f"the {self.provider} OAuth client is not configured")
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http or shared_client()

    def _post_token(self, data: dict[str, str]) -> dict[str, Any]:
        response = self._http.post(
            self.token_endpoint,
            data={**data, "client_id": self._client_id, "client_secret": self._client_secret},
            headers={"Accept": "application/json"},
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code >= 400:
            error = payload.get("error", "")
            description = payload.get("error_description") or response.text
            if error in {"invalid_grant", "unauthorized_client", "consent_required"}:
                raise ReauthRequired(f"{self.provider} refused the credential: {description}")
            raise ConnectorError(
                f"{self.provider} token endpoint returned HTTP {response.status_code}: {description}"
            )
        if "access_token" not in payload:
            raise ConnectorError(f"{self.provider} token response carried no access token")
        return payload

    def _token_set(self, payload: dict[str, Any], *, fallback_refresh: str | None = None) -> TokenSet:
        expires_in = int(payload.get("expires_in") or 3600)
        claims = decode_id_token_claims(payload.get("id_token"))
        return TokenSet(
            access_token=payload["access_token"],
            # Google omits refresh_token on refresh; keeping the old one is the
            # difference between a working connection and a weekly reconnect.
            refresh_token=payload.get("refresh_token") or fallback_refresh,
            expires_at=_dt.datetime.now(_dt.UTC) + _dt.timedelta(seconds=expires_in),
            scopes=tuple(str(payload.get("scope") or "").split()),
            address=self._address_from(claims, payload),
            claims=claims,
        )

    def _address_from(self, claims: dict[str, Any], payload: dict[str, Any]) -> str | None:
        for key in ("preferred_username", "email", "upn", "unique_name"):
            value = claims.get(key)
            if isinstance(value, str) and "@" in value:
                return value.strip().lower()
        return None


class MicrosoftOAuth(_OAuthClient):
    """Delegated Microsoft Graph access. ``Mail.Read``, never ``Mail.ReadWrite``."""

    provider = "graph"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        authority: str = "https://login.microsoftonline.com/common",
        scopes: tuple[str, ...] | list[str] = (),
        http: httpx.Client | None = None,
    ):
        self.authority = authority.rstrip("/")
        self.token_endpoint = f"{self.authority}/oauth2/v2.0/token"
        self.authorize_endpoint = f"{self.authority}/oauth2/v2.0/authorize"
        self.scopes = tuple(scopes) or ("offline_access", "User.Read", "Mail.Read")
        super().__init__(client_id, client_secret, http=http)

    @classmethod
    def from_settings(cls, settings: Settings, *, http: httpx.Client | None = None) -> MicrosoftOAuth:
        return cls(
            settings.ms_client_id or "",
            settings.ms_client_secret.get_secret_value() if settings.ms_client_secret else "",
            authority=settings.ms_authority,
            scopes=tuple(settings.ms_scope_list),
            http=http,
        )

    def authorization_url(self, *, redirect_uri: str, state: str, code_challenge: str) -> str:
        params = {
            "client_id": self._client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "response_mode": "query",
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        return f"{self.authorize_endpoint}?{httpx.QueryParams(params)}"

    def exchange_code(self, code: str, *, redirect_uri: str, code_verifier: str) -> TokenSet:
        return self._token_set(
            self._post_token(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                    "scope": " ".join(self.scopes),
                }
            )
        )

    def refresh(self, refresh_token: str) -> TokenSet:
        return self._token_set(
            self._post_token(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "scope": " ".join(self.scopes),
                }
            ),
            fallback_refresh=refresh_token,
        )


class GoogleOAuth(_OAuthClient):
    """Delegated Gmail access. ``gmail.readonly`` and nothing else.

    ``access_type=offline`` with ``prompt=consent`` is what produces a refresh
    token at all; without it Google returns one only on the very first consent,
    and a reconnect after a revocation would silently yield a connection that
    dies in an hour.
    """

    provider = "gmail"
    token_endpoint = GOOGLE_TOKEN_ENDPOINT

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        scopes: tuple[str, ...] | list[str] = (),
        hosted_domain: str | None = None,
        http: httpx.Client | None = None,
    ):
        self.scopes = tuple(scopes) or ("https://www.googleapis.com/auth/gmail.readonly",)
        self.hosted_domain = hosted_domain
        super().__init__(client_id, client_secret, http=http)

    @classmethod
    def from_settings(cls, settings: Settings, *, http: httpx.Client | None = None) -> GoogleOAuth:
        return cls(
            settings.google_client_id or "",
            settings.google_client_secret.get_secret_value()
            if settings.google_client_secret
            else "",
            scopes=tuple(settings.google_scope_list),
            hosted_domain=settings.google_hosted_domain,
            http=http,
        )

    def authorization_url(self, *, redirect_uri: str, state: str, code_challenge: str) -> str:
        params = {
            "client_id": self._client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(("openid", "email", *self.scopes)),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
        }
        if self.hosted_domain:
            params["hd"] = self.hosted_domain
        return f"{GOOGLE_AUTH_ENDPOINT}?{httpx.QueryParams(params)}"

    def exchange_code(self, code: str, *, redirect_uri: str, code_verifier: str) -> TokenSet:
        return self._token_set(
            self._post_token(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                }
            )
        )

    def refresh(self, refresh_token: str) -> TokenSet:
        return self._token_set(
            self._post_token({"grant_type": "refresh_token", "refresh_token": refresh_token}),
            fallback_refresh=refresh_token,
        )

    def revoke(self, token: str) -> bool:
        """Best effort. A revocation we cannot confirm is still worth attempting."""
        try:
            response = self._http.post(GOOGLE_REVOKE_ENDPOINT, data={"token": token})
        except httpx.HTTPError:
            return False
        return response.status_code < 400


def redirect_uri_for(provider: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return f"{settings.base_url.rstrip('/')}/oauth/callback/{provider}"


def build_oauth_client(
    provider: str, settings: Settings | None = None, *, http: httpx.Client | None = None
) -> MicrosoftOAuth | GoogleOAuth:
    settings = settings or get_settings()
    if provider == "graph":
        return MicrosoftOAuth.from_settings(settings, http=http)
    if provider == "gmail":
        return GoogleOAuth.from_settings(settings, http=http)
    raise ConfigError(f"{provider!r} does not use OAuth; IMAP uses an app password")


__all__ = [
    "STATE_MAX_AGE_SECONDS",
    "GoogleOAuth",
    "MicrosoftOAuth",
    "StateSigner",
    "TokenSet",
    "build_oauth_client",
    "code_challenge_for",
    "decode_id_token_claims",
    "new_code_verifier",
    "redirect_uri_for",
]
