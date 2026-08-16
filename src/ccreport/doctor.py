"""Configuration checks, with the remedy attached.

``ccreport doctor`` exists because three of this application's failure modes are
external and silent: a Google OAuth client in the wrong posture expires refresh
tokens weekly, an Entra app without admin consent fails per-user in a way that
looks like a bug, and an empty allow-list denies everyone while every log line
says the application is healthy.

Every check therefore carries what to do about it, in the form of the request to
send, rather than a status word that leaves the reader to guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .settings import Settings, get_settings

Status = Literal["ok", "warn", "fail"]

_RANK = {"ok": 0, "warn": 1, "fail": 2}


@dataclass(slots=True)
class Check:
    name: str
    status: Status
    detail: str
    remedy: str | None = None

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "remedy": self.remedy,
        }


@dataclass(slots=True)
class Diagnosis:
    checks: list[Check] = field(default_factory=list)

    @property
    def status(self) -> Status:
        return max((c.status for c in self.checks), key=lambda s: _RANK[s], default="ok")

    @property
    def ok(self) -> bool:
        return self.status != "fail"

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "ok": self.ok,
            "counts": {
                s: sum(1 for c in self.checks if c.status == s) for s in ("ok", "warn", "fail")
            },
            "checks": [c.as_dict() for c in self.checks],
        }


def _access_checks(settings: Settings, allow_list_count: int | None) -> list[Check]:
    checks: list[Check] = []

    if settings.required_email_domain:
        checks.append(
            Check("access.domain", "ok", f"first gate: @{settings.required_email_domain}")
        )
    else:
        checks.append(
            Check(
                "access.domain",
                "fail",
                "CCREPORT_REQUIRED_EMAIL_DOMAIN is empty, so the domain gate rejects everyone",
                "Set CCREPORT_REQUIRED_EMAIL_DOMAIN=princeton.edu.",
            )
        )

    seeded = len(settings.allowed_principal_list)
    # A seed that has not been applied yet still means access: the allow-list is
    # reconciled from settings at every startup. Only the case where neither the
    # table nor the settings name anybody is a denial of everyone.
    granted = allow_list_count or 0
    if granted or seeded:
        detail = f"{granted} principal(s) in the allow-list table"
        if seeded:
            detail += f", {seeded} seeded from settings and applied at startup"
        checks.append(Check("access.allow_list", "ok", detail))
    else:
        checks.append(
            Check(
                "access.allow_list",
                "fail",
                "the allow-list is empty, so every authenticated principal is denied",
                "Add people with `ccreport admin allow add UPN`, or seed "
                "CCREPORT_ALLOWED_PRINCIPALS and redeploy.",
            )
        )

    admins = len(settings.admin_principal_list)
    checks.append(
        Check(
            "access.admins",
            "ok" if admins else "warn",
            f"{admins} seeded administrator principal(s)",
            None if admins else "Nobody can reach the admin console; set CCREPORT_ADMIN_PRINCIPALS.",
        )
    )

    if settings.effective_dev_principal:
        checks.append(
            Check(
                "access.dev_principal",
                "warn",
                f"the Easy Auth bypass is active as {settings.effective_dev_principal}",
                "Expected locally. It is refused on Azure and in production.",
            )
        )
    return checks


def _secret_checks(settings: Settings) -> list[Check]:
    checks: list[Check] = []

    if settings.session_secret:
        checks.append(Check("secrets.session", "ok", "session secret is set"))
    else:
        checks.append(
            Check(
                "secrets.session",
                "fail" if settings.on_azure or settings.is_production else "warn",
                "CCREPORT_SESSION_SECRET is not set, so OAuth state cannot be signed",
                "Set CCREPORT_SESSION_SECRET to 32+ random bytes of hex.",
            )
        )

    if settings.keyvault_url:
        checks.append(
            Check(
                "secrets.wrapping",
                "ok",
                f"credentials wrapped by {settings.keyvault_key_name} in Key Vault",
            )
        )
    elif settings.dev_encryption_key:
        checks.append(
            Check(
                "secrets.wrapping",
                "fail" if settings.on_azure or settings.is_production else "warn",
                "credentials wrapped by a local development key",
                "Set CCREPORT_KEYVAULT_URL before any real mailbox is connected.",
            )
        )
    else:
        checks.append(
            Check(
                "secrets.wrapping",
                "fail",
                "no credential wrapping key is configured; connecting a mailbox will refuse",
                "Set CCREPORT_KEYVAULT_URL, or CCREPORT_DEV_ENCRYPTION_KEY locally "
                "(`ccreport doctor --generate-key` prints one).",
            )
        )
    return checks


def _storage_checks(settings: Settings) -> list[Check]:
    if settings.blob_account_url:
        return [
            Check(
                "storage.artifacts",
                "ok",
                f"artifacts in {settings.blob_container} at {settings.blob_account_url}",
            )
        ]
    return [
        Check(
            "storage.artifacts",
            "fail" if settings.on_azure or settings.is_production else "ok",
            "artifacts are written to local disk",
            "Set CCREPORT_BLOB_ACCOUNT_URL; App Service storage is not durable."
            if settings.on_azure or settings.is_production
            else None,
        )
    ]


def _connector_checks(settings: Settings) -> list[Check]:
    checks: list[Check] = []

    if settings.graph_configured:
        scopes = " ".join(settings.ms_scope_list)
        writes = [s for s in settings.ms_scope_list if "ReadWrite" in s or s.endswith(".Send")]
        checks.append(
            Check(
                "connector.graph",
                "fail" if writes else "ok",
                f"Microsoft Graph configured with: {scopes}",
                f"Remove the write scope(s) {' '.join(writes)}; ccreport reads only."
                if writes
                else None,
            )
        )
    else:
        checks.append(
            Check(
                "connector.graph",
                "warn",
                "Microsoft Graph is not configured; institutional Outlook cannot be connected",
                "Register the mailbox app in Entra with delegated Mail.Read. "
                "See docs/OIT-REQUESTS.md.",
            )
        )

    if settings.google_configured:
        posture = settings.google_oauth_publishing_status
        if settings.google_posture_is_durable:
            checks.append(
                Check("connector.google", "ok", f"Gmail configured; consent posture '{posture}'")
            )
        else:
            checks.append(
                Check(
                    "connector.google",
                    "warn",
                    f"Gmail consent posture is '{posture}', so refresh tokens expire after 7 days",
                    "Move the GCP project into the princeton.edu Cloud Organization "
                    "(Internal), or ask the Workspace administrator to mark the app "
                    "Trusted. See docs/OIT-REQUESTS.md.",
                )
            )
    else:
        checks.append(
            Check(
                "connector.google",
                "warn",
                "Gmail is not configured; institutional Workspace cannot be connected",
                "Create the OAuth client with gmail.readonly. See docs/OIT-REQUESTS.md.",
            )
        )

    checks.append(
        Check(
            "connector.personal_gmail",
            "ok",
            "personal Gmail over OAuth is off; the IMAP app-password path is the supported route"
            if not settings.enable_personal_gmail_oauth
            else "personal Gmail over OAuth is on and the client is declared verified",
        )
    )

    checks.append(
        Check(
            "connector.imap",
            "ok" if settings.enable_imap else "warn",
            f"IMAP {'enabled' if settings.enable_imap else 'disabled'} "
            f"({settings.imap_default_host}:{settings.imap_default_port}, EXAMINE only)",
            None if settings.enable_imap else "Personal Gmail cannot be connected at all.",
        )
    )
    return checks


def _render_checks(settings: Settings) -> list[Check]:
    from .render import available_renderers

    renderers = available_renderers()
    if not renderers:
        return [
            Check(
                "render.pdf",
                "fail",
                "no PDF renderer is available; messages without attachments cannot be submitted",
                "Install ccreport[render] (WeasyPrint) and its Cairo/Pango system libraries.",
            )
        ]
    status: Status = "ok"
    remedy = None
    if "weasyprint" not in renderers:
        status = "warn"
        remedy = "WeasyPrint is the primary renderer; only the fallback is present."
    elif settings.enable_playwright_fallback and "playwright" not in renderers:
        status = "warn"
        remedy = (
            "The Chromium fallback is enabled but not installed; difficult layouts "
            "will fail instead of falling back. Install ccreport[playwright]."
        )
    return [Check("render.pdf", status, f"renderers available: {', '.join(renderers)}", remedy)]


def _database_check(settings: Settings) -> Check:
    from sqlalchemy import text

    from .db import get_engine

    try:
        with get_engine(settings).connect() as connection:
            connection.execute(text("select 1"))
    except Exception as exc:
        return Check(
            "database.connection",
            "fail",
            f"cannot reach the database: {exc.__class__.__name__}: {exc}",
            "Check CCREPORT_DATABASE_URL, and that the server allows this address.",
        )
    return Check("database.connection", "ok", "database reachable")


def diagnose(
    settings: Settings | None = None,
    *,
    allow_list_count: int | None = None,
    check_database: bool = False,
) -> Diagnosis:
    """Run every check. ``allow_list_count`` comes from the table when available."""
    settings = settings or get_settings()
    checks: list[Check] = [
        Check(
            "environment",
            "ok",
            f"environment={settings.environment} on_azure={settings.on_azure} "
            f"month_window={settings.month_window}",
        )
    ]
    checks += _access_checks(settings, allow_list_count)
    checks += _secret_checks(settings)
    checks += _storage_checks(settings)
    checks += _connector_checks(settings)
    checks += _render_checks(settings)
    if check_database:
        checks.append(_database_check(settings))
    return Diagnosis(checks)


def connector_posture(settings: Settings | None = None) -> dict:
    """The payload behind ``/api/connectors/posture``.

    Deployment verification and the accounts page both need the same answer:
    which providers can be connected right now, and what will go wrong if they are.
    """
    settings = settings or get_settings()
    from .connectors.registry import describe_provider

    providers = []
    for name in ("graph", "gmail", "imap"):
        described = describe_provider(name, settings)
        if name == "gmail":
            described["posture"] = settings.google_oauth_publishing_status
            described["durable_refresh_tokens"] = settings.google_posture_is_durable
            described["personal_gmail_oauth"] = settings.enable_personal_gmail_oauth
        providers.append(described)
    diagnosis = diagnose(settings)
    return {
        "status": diagnosis.status,
        "providers": providers,
        "checks": [c.as_dict() for c in diagnosis.checks if c.name.startswith("connector.")],
    }


__all__ = ["Check", "Diagnosis", "Status", "connector_posture", "diagnose"]
