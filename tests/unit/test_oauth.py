from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from ccreport.errors import ConfigError, ConnectorError, ReauthRequired
from ccreport.oauth import (
    GoogleOAuth,
    MicrosoftOAuth,
    StateSigner,
    build_oauth_client,
    code_challenge_for,
    decode_id_token_claims,
    new_code_verifier,
    redirect_uri_for,
)
from ccreport.settings import Settings


def id_token(claims: dict) -> str:
    def segment(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{segment({'alg': 'RS256'})}.{segment(claims)}.signature"


class FakeTokenEndpoint:
    def __init__(self, payload: dict | None = None, status: int = 200) -> None:
        self.payload = payload or {
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "expires_in": 3600,
            "scope": "Mail.Read offline_access",
            "id_token": id_token({"preferred_username": "Ada@Princeton.edu", "name": "Ada"}),
        }
        self.status = status
        self.requests: list[dict] = []

    def client(self) -> httpx.Client:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(dict(parse_qs(request.content.decode())))
            return httpx.Response(self.status, json=self.payload)

        return httpx.Client(transport=httpx.MockTransport(handle))


def microsoft(endpoint: FakeTokenEndpoint) -> MicrosoftOAuth:
    return MicrosoftOAuth(
        "ms-client", "ms-secret", scopes=("offline_access", "Mail.Read"), http=endpoint.client()
    )


def google(endpoint: FakeTokenEndpoint) -> GoogleOAuth:
    return GoogleOAuth(
        "g-client", "g-secret", hosted_domain="princeton.edu", http=endpoint.client()
    )


# ----------------------------------------------------------------------- PKCE
def test_code_challenge_is_the_s256_of_the_verifier() -> None:
    import hashlib

    verifier = new_code_verifier()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert code_challenge_for(verifier) == expected
    assert "=" not in code_challenge_for(verifier)


def test_verifiers_are_not_reused() -> None:
    assert new_code_verifier() != new_code_verifier()


# ---------------------------------------------------------------------- state
def test_state_round_trips_and_carries_the_verifier() -> None:
    signer = StateSigner("secret")
    token = signer.sign({"upn": "ada@princeton.edu", "provider": "graph", "verifier": "v"})
    payload = signer.unsign(token)
    assert payload["upn"] == "ada@princeton.edu"
    assert payload["verifier"] == "v"


def test_state_signed_with_another_secret_is_refused() -> None:
    token = StateSigner("secret-a").sign({"upn": "ada@princeton.edu"})
    with pytest.raises(ConnectorError, match="did not carry a state value we issued"):
        StateSigner("secret-b").unsign(token)


def test_expired_state_is_refused_with_an_actionable_message() -> None:
    signer = StateSigner("secret")
    token = signer.sign({"upn": "ada@princeton.edu"})
    with pytest.raises(ConnectorError, match="expired"):
        signer.unsign(token, max_age=-1)


def test_signing_without_a_session_secret_refuses_rather_than_signing_with_nothing() -> None:
    with pytest.raises(ConfigError, match="CCREPORT_SESSION_SECRET"):
        StateSigner("")


# ------------------------------------------------------- authorization request
def test_microsoft_authorization_url_requests_read_scopes_with_pkce() -> None:
    url = microsoft(FakeTokenEndpoint()).authorization_url(
        redirect_uri="https://app.example/oauth/callback/graph", state="st", code_challenge="ch"
    )
    query = parse_qs(urlparse(url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == ["ch"]
    assert query["redirect_uri"] == ["https://app.example/oauth/callback/graph"]
    assert "Mail.Read" in query["scope"][0]
    assert "Mail.ReadWrite" not in query["scope"][0]


def test_google_authorization_url_asks_for_offline_consent_and_the_hosted_domain() -> None:
    url = google(FakeTokenEndpoint()).authorization_url(
        redirect_uri="https://app.example/oauth/callback/gmail", state="st", code_challenge="ch"
    )
    query = parse_qs(urlparse(url).query)
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["hd"] == ["princeton.edu"]
    assert query["scope"][0].endswith("gmail.readonly")


# ------------------------------------------------------------- token exchange
def test_exchange_sends_the_verifier_and_normalises_the_response() -> None:
    endpoint = FakeTokenEndpoint()
    tokens = microsoft(endpoint).exchange_code(
        "code-1", redirect_uri="https://app.example/cb", code_verifier="verifier-1"
    )
    assert endpoint.requests[0]["code_verifier"] == ["verifier-1"]
    assert endpoint.requests[0]["grant_type"] == ["authorization_code"]
    assert tokens.access_token == "at-1"
    assert tokens.refresh_token == "rt-1"
    assert tokens.address == "ada@princeton.edu"  # normalised to lowercase
    assert tokens.expires_in() > 3500


def test_refresh_keeps_the_old_refresh_token_when_google_omits_one() -> None:
    """Google returns no refresh_token on refresh; dropping it costs a reconnect."""
    endpoint = FakeTokenEndpoint({"access_token": "at-2", "expires_in": 3599})
    tokens = google(endpoint).refresh("rt-original")
    assert tokens.access_token == "at-2"
    assert tokens.refresh_token == "rt-original"


def test_invalid_grant_is_reported_as_needing_reauthorization() -> None:
    endpoint = FakeTokenEndpoint(
        {"error": "invalid_grant", "error_description": "token revoked"}, status=400
    )
    with pytest.raises(ReauthRequired, match="token revoked"):
        google(endpoint).refresh("rt-dead")


def test_other_token_errors_are_connector_errors() -> None:
    endpoint = FakeTokenEndpoint({"error": "server_error"}, status=500)
    with pytest.raises(ConnectorError, match="HTTP 500"):
        microsoft(endpoint).refresh("rt")


def test_a_token_response_without_an_access_token_is_refused() -> None:
    endpoint = FakeTokenEndpoint({"token_type": "Bearer"})
    with pytest.raises(ConnectorError, match="no access token"):
        microsoft(endpoint).exchange_code("c", redirect_uri="r", code_verifier="v")


# ------------------------------------------------------------------ id tokens
def test_id_token_claims_are_read_without_verification_and_never_crash() -> None:
    assert decode_id_token_claims(id_token({"email": "ada@princeton.edu"}))["email"] == "ada@princeton.edu"
    assert decode_id_token_claims(None) == {}
    assert decode_id_token_claims("not-a-jwt") == {}
    assert decode_id_token_claims("a.b.c") == {}


# --------------------------------------------------------------------- wiring
def test_redirect_uri_is_derived_from_the_base_url() -> None:
    settings = Settings(base_url="https://ccreport.example.edu/")
    assert redirect_uri_for("graph", settings) == "https://ccreport.example.edu/oauth/callback/graph"


def test_imap_has_no_oauth_client() -> None:
    with pytest.raises(ConfigError, match="app password"):
        build_oauth_client("imap", Settings())


def test_an_unconfigured_provider_refuses_to_build(settings: Settings) -> None:
    bare = settings.model_copy(update={"ms_client_id": None})
    with pytest.raises(ConfigError, match="not configured"):
        build_oauth_client("graph", bare)
