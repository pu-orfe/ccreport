"""Authentication and authorization for ccreport."""

from __future__ import annotations

from .allowlist import (
    ROLE_ADMIN,
    ROLE_FACULTY,
    ROLES,
    DomainDenied,
    InsufficientRole,
    NotOnAllowList,
    audit,
    authorize,
    bootstrap_allow_list,
    domain_allowed,
    grant_access,
    has_role,
    list_access,
    lookup_grant,
    revoke_access,
)
from .principal import (
    InconsistentPrincipal,
    Principal,
    PrincipalError,
    decode_claims_header,
    dev_principal,
    principal_from_headers,
    resolve_principal,
)

__all__ = [
    "ROLES",
    "ROLE_ADMIN",
    "ROLE_FACULTY",
    "DomainDenied",
    "InconsistentPrincipal",
    "InsufficientRole",
    "NotOnAllowList",
    "Principal",
    "PrincipalError",
    "audit",
    "authorize",
    "bootstrap_allow_list",
    "decode_claims_header",
    "dev_principal",
    "domain_allowed",
    "grant_access",
    "has_role",
    "list_access",
    "lookup_grant",
    "principal_from_headers",
    "resolve_principal",
    "revoke_access",
]
