"""The access gates.

These tests exist because every one of them describes a way the application
could silently become public.
"""

from __future__ import annotations

import base64
import json

import pytest
from sqlalchemy.orm import Session

from ccreport.auth import (
    DomainDenied,
    InconsistentPrincipal,
    InsufficientRole,
    NotOnAllowList,
    Principal,
    PrincipalError,
    authorize,
    bootstrap_allow_list,
    decode_claims_header,
    domain_allowed,
    grant_access,
    list_access,
    principal_from_headers,
    resolve_principal,
    revoke_access,
)
from ccreport.errors import ConfigError
from ccreport.settings import Settings

UPN = "ada@princeton.edu"


def make_settings(**overrides) -> Settings:
    base = {
        "required_email_domain": "princeton.edu",
        "allowed_principals": "",
        "admin_principals": "",
        "database_url": "sqlite://",
        # Required in production, and harmless everywhere else.
        "session_secret": "test-only-session-secret",
    }
    base.update(overrides)
    return Settings(**base)


def claims_header(upn: str = UPN, *, name: str = "Ada Lovelace", extra=None) -> str:
    document = {
        "auth_typ": "aad",
        "name_typ": "preferred_username",
        "claims": [
            {"typ": "preferred_username", "val": upn},
            {"typ": "name", "val": name},
            *(extra or []),
        ],
    }
    return base64.b64encode(json.dumps(document).encode()).decode()


def easy_auth_headers(upn: str = UPN, **kwargs) -> dict[str, str]:
    return {
        "X-MS-CLIENT-PRINCIPAL-NAME": upn,
        "X-MS-CLIENT-PRINCIPAL": claims_header(upn, **kwargs),
        "X-MS-CLIENT-PRINCIPAL-IDP": "aad",
    }


# --------------------------------------------------------------------- headers


def test_principal_parsed_from_both_headers():
    principal = principal_from_headers(easy_auth_headers(), settings=make_settings())
    assert principal.upn == UPN
    assert principal.display_name == "Ada Lovelace"
    assert principal.provider == "aad"
    assert principal.is_dev is False


def test_name_header_alone_is_refused():
    """The single spoofable header is not an authentication decision."""
    with pytest.raises(PrincipalError, match="no X-MS-CLIENT-PRINCIPAL header"):
        principal_from_headers(
            {"X-MS-CLIENT-PRINCIPAL-NAME": UPN}, settings=make_settings()
        )


def test_disagreeing_headers_are_refused():
    headers = easy_auth_headers()
    headers["X-MS-CLIENT-PRINCIPAL-NAME"] = "mallory@princeton.edu"
    with pytest.raises(InconsistentPrincipal):
        principal_from_headers(headers, settings=make_settings())


def test_malformed_claims_blob_is_refused():
    with pytest.raises(PrincipalError):
        principal_from_headers(
            {"X-MS-CLIENT-PRINCIPAL-NAME": UPN, "X-MS-CLIENT-PRINCIPAL": "not-base64-json!!"},
            settings=make_settings(),
        )


def test_claims_blob_without_identity_claim_is_refused():
    document = base64.b64encode(json.dumps({"claims": [{"typ": "roles", "val": "x"}]}).encode()).decode()
    with pytest.raises(PrincipalError):
        principal_from_headers({"X-MS-CLIENT-PRINCIPAL": document}, settings=make_settings())


def test_guest_upn_is_unmangled():
    """Entra presents guests as ada_example.edu#EXT#@princeton.edu."""
    mangled = "ada_example.edu#EXT#@princeton.edu"
    principal = principal_from_headers(
        {
            "X-MS-CLIENT-PRINCIPAL-NAME": mangled,
            "X-MS-CLIENT-PRINCIPAL": claims_header(mangled),
        },
        settings=make_settings(),
    )
    assert principal.upn == "ada@example.edu"


def test_upn_is_case_normalised():
    principal = principal_from_headers(easy_auth_headers("Ada@Princeton.EDU"), settings=make_settings())
    assert principal.upn == "ada@princeton.edu"


def test_decode_claims_header_tolerates_missing_padding():
    raw = base64.b64encode(json.dumps({"claims": []}).encode()).decode().rstrip("=")
    assert decode_claims_header(raw) == {}


# ----------------------------------------------------------------- dev bypass


def test_dev_principal_used_only_when_headers_absent(monkeypatch):
    monkeypatch.delenv("WEBSITE_SITE_NAME", raising=False)
    settings = make_settings(dev_principal="dev@princeton.edu", environment="development")
    principal = resolve_principal({}, settings=settings)
    assert principal.upn == "dev@princeton.edu"
    assert principal.is_dev is True


def test_dev_principal_refused_on_azure(monkeypatch):
    """Settings must refuse to construct rather than serve unauthenticated."""
    monkeypatch.setenv("WEBSITE_SITE_NAME", "ccreport-prod")
    with pytest.raises(ConfigError, match="bypass Entra"):
        make_settings(dev_principal="dev@princeton.edu")


def test_dev_principal_ignored_in_production(monkeypatch):
    monkeypatch.delenv("WEBSITE_SITE_NAME", raising=False)
    settings = make_settings(dev_principal="dev@princeton.edu", environment="production")
    assert settings.effective_dev_principal is None
    with pytest.raises(PrincipalError):
        resolve_principal({}, settings=settings)


def test_inconsistent_headers_are_not_rescued_by_dev_bypass(monkeypatch):
    monkeypatch.delenv("WEBSITE_SITE_NAME", raising=False)
    settings = make_settings(dev_principal="dev@princeton.edu", environment="development")
    headers = easy_auth_headers()
    headers["X-MS-CLIENT-PRINCIPAL-NAME"] = "mallory@princeton.edu"
    with pytest.raises(InconsistentPrincipal):
        resolve_principal(headers, settings=settings)


# --------------------------------------------------------------------- gate 1


@pytest.mark.parametrize(
    ("upn", "expected"),
    [
        ("ada@princeton.edu", True),
        ("ada@PRINCETON.EDU", True),
        ("ada@cs.princeton.edu", False),
        ("ada@notprinceton.edu", False),
        ("ada@gmail.com", False),
        ("adaprinceton.edu", False),
    ],
)
def test_domain_gate(upn, expected):
    assert domain_allowed(upn, make_settings()) is expected


def test_empty_required_domain_denies_rather_than_widens():
    assert domain_allowed(UPN, make_settings(required_email_domain="")) is False


# --------------------------------------------------------------------- gate 2


def test_empty_allow_list_denies_everyone(db_session: Session):
    settings = make_settings()
    bootstrap_allow_list(db_session, settings)
    assert list_access(db_session) == []
    with pytest.raises(NotOnAllowList):
        authorize(db_session, Principal(upn=UPN), settings)


def test_allow_list_admits_a_seeded_principal(db_session: Session):
    settings = make_settings(allowed_principals=f"{UPN}, bob@princeton.edu")
    bootstrap_allow_list(db_session, settings)
    user = authorize(db_session, Principal(upn=UPN, display_name="Ada"), settings)
    assert user.upn == UPN
    assert user.role == "faculty"
    assert user.last_seen_at is not None


def test_admin_principals_are_admitted_and_promoted(db_session: Session):
    settings = make_settings(allowed_principals=UPN, admin_principals="root@princeton.edu")
    bootstrap_allow_list(db_session, settings)
    admin = authorize(db_session, Principal(upn="root@princeton.edu"), settings)
    assert admin.role == "admin"
    faculty = authorize(db_session, Principal(upn=UPN), settings)
    with pytest.raises(InsufficientRole):
        authorize(db_session, Principal(upn=faculty.upn), settings, require_role="admin")


def test_outside_domain_is_denied_before_the_allow_list(db_session: Session):
    settings = make_settings(allowed_principals="ada@gmail.com")
    bootstrap_allow_list(db_session, settings)
    with pytest.raises(DomainDenied):
        authorize(db_session, Principal(upn="ada@gmail.com"), settings)


def test_manual_grant_survives_reseeding(db_session: Session):
    """A redeploy must not silently revoke somebody an administrator added."""
    settings = make_settings(allowed_principals=UPN)
    bootstrap_allow_list(db_session, settings)
    grant_access(db_session, "carol@princeton.edu", added_by="root@princeton.edu")
    db_session.flush()

    bootstrap_allow_list(db_session, settings)
    remaining = {row.upn for row in list_access(db_session)}
    assert remaining == {UPN, "carol@princeton.edu"}


def test_reseeding_removes_a_principal_dropped_from_settings(db_session: Session):
    bootstrap_allow_list(db_session, make_settings(allowed_principals=f"{UPN},bob@princeton.edu"))
    db_session.flush()
    bootstrap_allow_list(db_session, make_settings(allowed_principals=UPN))
    assert {row.upn for row in list_access(db_session)} == {UPN}


def test_revoking_access_denies_the_next_request(db_session: Session):
    settings = make_settings()
    grant_access(db_session, UPN, added_by="root@princeton.edu")
    db_session.flush()
    authorize(db_session, Principal(upn=UPN), settings)

    assert revoke_access(db_session, UPN, removed_by="root@princeton.edu") is True
    db_session.flush()
    with pytest.raises(NotOnAllowList):
        authorize(db_session, Principal(upn=UPN), settings)


def test_role_change_on_the_grant_wins_over_the_cached_column(db_session: Session):
    settings = make_settings()
    grant_access(db_session, UPN)
    db_session.flush()
    user = authorize(db_session, Principal(upn=UPN), settings)
    assert user.role == "faculty"

    grant_access(db_session, UPN, role="admin")
    db_session.flush()
    user = authorize(db_session, Principal(upn=UPN), settings)
    assert user.role == "admin"
