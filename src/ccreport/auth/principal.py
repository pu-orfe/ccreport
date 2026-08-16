"""Azure App Service Easy Auth, parsed with suspicion.

Easy Auth sets several headers on every authenticated request. The obvious one,
``X-MS-CLIENT-PRINCIPAL-NAME``, is a plain string, and a plain string set by a
reverse proxy is only as trustworthy as the guarantee that nothing can reach the
origin except through that proxy. A misconfigured ingress, a container port
exposed directly, or a private-endpoint mistake all break that guarantee, and
they break it silently.

So this module demands ``X-MS-CLIENT-PRINCIPAL`` as well — the base64 JSON claims
blob Easy Auth sets alongside the name — and requires the two to agree. That does
not make header spoofing impossible; it raises the bar from "set one header" to
"forge a well-formed claims document that agrees with itself", and it makes the
inconsistent case loud rather than silently authenticated.

Nothing here trusts the client to tell us their role. Roles come from the
allow-list in our own database, never from a claim.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from ..errors import ConfigError
from ..settings import Settings

logger = logging.getLogger("ccreport.auth")

HEADER_PRINCIPAL = "x-ms-client-principal"
HEADER_PRINCIPAL_NAME = "x-ms-client-principal-name"
HEADER_PRINCIPAL_ID = "x-ms-client-principal-id"
HEADER_PRINCIPAL_IDP = "x-ms-client-principal-idp"

#: Claim types that carry an email address or UPN, most specific first.
_EMAIL_CLAIMS = (
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn",
    "preferred_username",
    "upn",
    "email",
    "emails",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
)

_NAME_CLAIMS = (
    "name",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
    "given_name",
)


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller, normalised."""

    upn: str
    display_name: str | None = None
    provider: str | None = None
    object_id: str | None = None
    claims: Mapping[str, str] = field(default_factory=dict)
    #: True when this principal came from the local development bypass rather
    #: than from Easy Auth. Surfaced in the UI so it cannot be mistaken for a
    #: real sign-in.
    is_dev: bool = False

    @property
    def domain(self) -> str:
        _, _, domain = self.upn.partition("@")
        return domain


class PrincipalError(Exception):
    """The request carried no usable, self-consistent identity."""


class InconsistentPrincipal(PrincipalError):
    """The name header and the claims blob disagree.

    Treated as an attack until proven otherwise: the only innocent explanation
    is a proxy misconfiguration, and that is also worth failing loudly for.
    """


def _normalise(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().lower()
    # Entra sometimes presents a guest UPN in the mangled form
    # ada_example.edu#EXT#@princeton.edu. The address before the marker is the
    # real one, and matching an allow-list against the mangled form would fail
    # in a way nobody would guess from the error.
    if "#ext#" in value:
        external, _, _ = value.partition("#ext#")
        value = external.replace("_", "@", 1) if "_" in external else external
    return value or None


def decode_claims_header(raw: str) -> dict[str, str]:
    """Decode the base64 JSON claims blob into a flat mapping.

    Raises :class:`PrincipalError` on anything malformed. A blob we cannot parse
    is not a blob we should authenticate.
    """
    try:
        padded = raw + "=" * (-len(raw) % 4)
        decoded = base64.b64decode(padded)
        document = json.loads(decoded)
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise PrincipalError(f"client principal header is not valid base64 JSON: {exc}") from exc

    if not isinstance(document, dict):
        raise PrincipalError("client principal header did not decode to an object")

    claims: dict[str, str] = {}
    for claim in document.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        typ, val = claim.get("typ"), claim.get("val")
        if isinstance(typ, str) and isinstance(val, str) and typ not in claims:
            claims[typ] = val

    # Easy Auth records which claim type it treats as the name; honour it.
    name_type = document.get("name_typ")
    if isinstance(name_type, str) and name_type in claims:
        claims.setdefault("_name_claim", claims[name_type])
    if isinstance(document.get("auth_typ"), str):
        claims.setdefault("_auth_typ", document["auth_typ"])
    return claims


def _claim_lookup(claims: Mapping[str, str], candidates: tuple[str, ...]) -> str | None:
    for key in candidates:
        value = claims.get(key)
        if value:
            return value
    return None


def principal_from_headers(
    headers: Mapping[str, str], *, settings: Settings, require_claims: bool = True
) -> Principal:
    """Build a :class:`Principal` from request headers.

    ``require_claims`` exists only so a test can exercise the weaker path; it is
    True everywhere in the application and there is no setting to change it.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    name_header = _normalise(lowered.get(HEADER_PRINCIPAL_NAME))
    claims_header = lowered.get(HEADER_PRINCIPAL)

    if not claims_header:
        if require_claims:
            # A name header with no claims blob is exactly what a spoofing
            # attempt looks like, and also what a bypassed proxy looks like.
            raise PrincipalError(
                "no X-MS-CLIENT-PRINCIPAL header. Either the request did not "
                "pass through Easy Auth, or authentication is not enforced on "
                "this App Service."
            )
        if not name_header:
            raise PrincipalError("request carried no client principal headers")
        return Principal(upn=name_header, provider=lowered.get(HEADER_PRINCIPAL_IDP))

    claims = decode_claims_header(claims_header)
    claim_upn = _normalise(_claim_lookup(claims, _EMAIL_CLAIMS))

    if not claim_upn and not name_header:
        raise PrincipalError("client principal carried no email or UPN claim")

    if name_header and claim_upn and name_header != claim_upn:
        raise InconsistentPrincipal(
            "X-MS-CLIENT-PRINCIPAL-NAME does not match the UPN in the claims "
            "blob. Refusing the request."
        )

    upn = claim_upn or name_header
    if not upn:  # unreachable: the checks above already narrowed this
        raise PrincipalError("client principal carried no email or UPN claim")

    return Principal(
        upn=upn,
        display_name=_claim_lookup(claims, _NAME_CLAIMS) or claims.get("_name_claim"),
        provider=lowered.get(HEADER_PRINCIPAL_IDP) or claims.get("_auth_typ"),
        object_id=lowered.get(HEADER_PRINCIPAL_ID)
        or claims.get("http://schemas.microsoft.com/identity/claims/objectidentifier"),
        claims=claims,
    )


def dev_principal(settings: Settings) -> Principal:
    """The local development identity.

    :meth:`Settings.effective_dev_principal` already returns None on Azure and in
    production, and :class:`Settings` refuses to construct at all if the variable
    is set while App Service is detected. This is the third check of the same
    thing, and it is here because the cost of being wrong is an unauthenticated
    application.
    """
    upn = settings.effective_dev_principal
    if not upn:
        raise ConfigError(
            "no development principal is available in this environment. "
            "CCREPORT_DEV_PRINCIPAL is ignored on Azure and in production."
        )
    return Principal(upn=upn, display_name="Local development", provider="dev", is_dev=True)


def resolve_principal(headers: Mapping[str, str], *, settings: Settings) -> Principal:
    """Easy Auth if present, the development bypass if permitted, else refuse."""
    try:
        return principal_from_headers(headers, settings=settings)
    except InconsistentPrincipal:
        raise
    except PrincipalError:
        if settings.effective_dev_principal:
            logger.warning(
                "using CCREPORT_DEV_PRINCIPAL; this must never happen outside development"
            )
            return dev_principal(settings)
        raise


__all__ = [
    "HEADER_PRINCIPAL",
    "HEADER_PRINCIPAL_ID",
    "HEADER_PRINCIPAL_IDP",
    "HEADER_PRINCIPAL_NAME",
    "InconsistentPrincipal",
    "Principal",
    "PrincipalError",
    "decode_claims_header",
    "dev_principal",
    "principal_from_headers",
    "resolve_principal",
]
