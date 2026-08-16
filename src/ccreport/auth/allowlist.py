"""The access list.

Two gates, in order:

1. The principal's domain must match ``required_email_domain``. This is the
   institutional boundary and it is not negotiable per-user.
2. The principal must appear in ``allowed_principals``. **An empty allow-list
   denies everyone**, including a validly authenticated Entra principal. There
   is no "empty means open" path in this module, and a test asserts it.

The second gate is a table rather than a setting so that adding somebody does not
require a redeploy. The setting seeds it; the table is authoritative afterwards.
"""

from __future__ import annotations

import datetime as _dt
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import NotAuthorized
from ..models import AllowedPrincipal, AuditLog, User
from ..settings import Settings
from .principal import Principal

logger = logging.getLogger("ccreport.auth")

ROLE_FACULTY = "faculty"
ROLE_ADMIN = "admin"
ROLES = (ROLE_FACULTY, ROLE_ADMIN)


class DomainDenied(NotAuthorized):
    """The principal is authenticated but outside the institution."""


class NotOnAllowList(NotAuthorized):
    """The principal is inside the institution but has not been granted access."""


class InsufficientRole(NotAuthorized):
    """The principal is allowed in, but not to do this."""


def domain_allowed(upn: str, settings: Settings) -> bool:
    required = settings.required_email_domain
    if not required:
        # An empty required domain would let anyone Entra authenticates through
        # the first gate. Refuse rather than quietly widening the boundary.
        return False
    return upn.strip().lower().endswith("@" + required)


def lookup_grant(session: Session, upn: str) -> AllowedPrincipal | None:
    return session.get(AllowedPrincipal, upn.strip().lower())


def bootstrap_allow_list(session: Session, settings: Settings) -> int:
    """Reconcile the seeded portion of the allow-list with settings.

    Seeded rows are marked as such, so a redeploy can add and remove them
    without touching a grant an administrator made by hand. Returns the number
    of rows added or changed.
    """
    seed_faculty = set(settings.allowed_principal_list)
    seed_admins = set(settings.admin_principal_list)
    # An admin who was never listed as allowed is still allowed; requiring both
    # lists to be maintained in step is a footgun with no upside.
    seed_faculty |= seed_admins

    changed = 0
    for upn in sorted(seed_faculty):
        role = ROLE_ADMIN if upn in seed_admins else ROLE_FACULTY
        existing = session.get(AllowedPrincipal, upn)
        if existing is None:
            session.add(
                AllowedPrincipal(upn=upn, role=role, added_by="settings", seeded=True, note="seeded from CCREPORT_ALLOWED_PRINCIPALS")
            )
            changed += 1
        elif existing.seeded and existing.role != role:
            existing.role = role
            changed += 1

    stale = session.scalars(
        select(AllowedPrincipal).where(AllowedPrincipal.seeded.is_(True))
    ).all()
    for row in stale:
        if row.upn not in seed_faculty:
            session.delete(row)
            changed += 1

    if changed:
        logger.info("allow-list reconciled from settings: %d change(s)", changed)
    return changed


def authorize(
    session: Session,
    principal: Principal,
    settings: Settings,
    *,
    require_role: str | None = None,
    touch: bool = True,
) -> User:
    """Apply both gates and return the :class:`User` row.

    Raises :class:`DomainDenied`, :class:`NotOnAllowList` or
    :class:`InsufficientRole`. The distinct exception types exist so the denial
    page can say something true and useful — "you are not on the list, ask X"
    is actionable, "403" is not.
    """
    upn = principal.upn.strip().lower()

    if not domain_allowed(upn, settings):
        raise DomainDenied(
            f"{upn} is outside {settings.required_email_domain}. "
            "ccreport is limited to institutional accounts."
        )

    grant = lookup_grant(session, upn)
    if grant is None:
        raise NotOnAllowList(
            f"{upn} is authenticated but not on the ccreport access list. "
            "An administrator must add you before you can sign in."
        )

    user = session.scalar(select(User).where(User.upn == upn))
    if user is None:
        user = User(upn=upn, display_name=principal.display_name, role=grant.role)
        session.add(user)
        session.flush()
    else:
        # The grant is authoritative; the column on User is a cache for joins.
        if user.role != grant.role:
            user.role = grant.role
        if principal.display_name and user.display_name != principal.display_name:
            user.display_name = principal.display_name

    if touch:
        user.last_seen_at = _dt.datetime.now(_dt.UTC)

    if require_role and not has_role(user, require_role):
        raise InsufficientRole(
            f"{upn} does not hold the '{require_role}' role required for this action."
        )

    return user


def has_role(user: User, role: str) -> bool:
    if role == ROLE_FACULTY:
        return user.role in ROLES
    return user.role == role


def grant_access(
    session: Session,
    upn: str,
    *,
    role: str = ROLE_FACULTY,
    added_by: str | None = None,
    note: str | None = None,
) -> AllowedPrincipal:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}, got {role!r}")
    upn = upn.strip().lower()
    existing = session.get(AllowedPrincipal, upn)
    if existing:
        existing.role = role
        existing.seeded = False
        if note:
            existing.note = note
        record = existing
    else:
        record = AllowedPrincipal(upn=upn, role=role, added_by=added_by, note=note, seeded=False)
        session.add(record)

    user = session.scalar(select(User).where(User.upn == upn))
    if user is not None:
        user.role = role

    audit(session, actor=added_by, action="allowlist.grant", subject_type="principal", subject_id=upn, detail={"role": role})
    return record


def revoke_access(session: Session, upn: str, *, removed_by: str | None = None) -> bool:
    upn = upn.strip().lower()
    existing = session.get(AllowedPrincipal, upn)
    if existing is None:
        return False
    session.delete(existing)
    audit(session, actor=removed_by, action="allowlist.revoke", subject_type="principal", subject_id=upn)
    return True


def list_access(session: Session) -> list[AllowedPrincipal]:
    return list(
        session.scalars(select(AllowedPrincipal).order_by(AllowedPrincipal.upn)).all()
    )


def audit(
    session: Session,
    *,
    actor: str | None,
    action: str,
    subject_type: str | None = None,
    subject_id: str | None = None,
    detail: dict | None = None,
) -> None:
    """Append to the audit log.

    Deliberately never raises into the caller's path: failing to record an
    action is bad, but failing a faculty member's submission because the audit
    insert hit a constraint would be worse.
    """
    try:
        session.add(
            AuditLog(
                actor_upn=(actor or "").strip().lower() or None,
                action=action,
                subject_type=subject_type,
                subject_id=subject_id,
                detail=detail,
            )
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("failed to write audit entry for %s", action)


__all__ = [
    "ROLES",
    "ROLE_ADMIN",
    "ROLE_FACULTY",
    "DomainDenied",
    "InsufficientRole",
    "NotOnAllowList",
    "audit",
    "authorize",
    "bootstrap_allow_list",
    "domain_allowed",
    "grant_access",
    "has_role",
    "list_access",
    "lookup_grant",
    "revoke_access",
]
