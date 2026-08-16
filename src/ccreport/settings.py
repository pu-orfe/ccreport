"""Application settings.

Lazy by design: importing this module has no side effects, so ``create_app()``
and the CLI can both be imported by a test that never intends to configure
anything.

Three rules here are load-bearing and are covered by tests rather than comments:

* An empty allow-list denies everyone. There is no "empty means open" path.
* ``dev_principal`` refuses to exist on Azure. Setting ``CCREPORT_DEV_PRINCIPAL``
  while ``WEBSITE_SITE_NAME`` is present raises :class:`ConfigError` at
  construction. Refusing to boot is the only guard nobody can ignore.
* Personal-Gmail OAuth cannot be switched on casually. ``gmail.readonly`` is a
  restricted scope; enabling it without declaring the OAuth client verified
  raises :class:`ConfigError`, because the alternative is an app that silently
  caps at 100 users and expires every refresh token after seven days.
"""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigError

#: Google deletes OAuth clients that see no token exchange for this long.
GOOGLE_IDLE_CLIENT_DELETION_DAYS = 180


def _split_list(value: str | list[str] | None) -> list[str]:
    """Accept a comma-separated string or a real list; normalise to lowercase."""
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.replace("\n", ",").split(",")
    else:
        parts = list(value)
    return [p.strip().lower() for p in parts if p and p.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CCREPORT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------------------------------------------------------- general
    environment: str = "development"
    log_level: str = "INFO"
    base_url: str = "http://localhost:8000"

    #: How far back the month picker may reach, inclusive of the current month.
    #: Clamped server-side, not merely in the UI.
    month_window: int = Field(default=8, ge=1, le=36)

    # ------------------------------------------------------------ access gate
    #: First gate. Every principal must match this domain to get past the door.
    required_email_domain: str = "princeton.edu"

    #: Seed for the allow-list table. Empty means DENY ALL — including a validly
    #: authenticated Entra principal.
    allowed_principals: str = ""

    #: Principals granted the admin role. Must also appear in the allow-list, or
    #: be seeded from it; `bootstrap_allow_list` handles that.
    admin_principals: str = ""

    #: Local development only. Refused outright when running on Azure.
    dev_principal: str | None = None

    # ------------------------------------------------------------------- data
    database_url: str = "postgresql+psycopg://ccreport:ccreport@localhost:5432/ccreport"

    #: Blob container for report artifacts. Empty means "write to local disk",
    #: which is the laptop and CI path.
    blob_account_url: str | None = None
    blob_container: str = "ccreport-artifacts"
    local_artifact_dir: str | None = None

    #: Key Vault key used to wrap per-record data keys. Empty means the
    #: development wrapper, which refuses to run outside development.
    keyvault_url: str | None = None
    keyvault_key_name: str = "ccreport-token-dek"
    #: Development-only symmetric key, base64. Never set this on Azure.
    dev_encryption_key: SecretStr | None = None

    #: Signs OAuth `state` and other short-lived round-trip values.
    session_secret: SecretStr | None = None

    # -------------------------------------------------------- header caching
    #: Minimal retention: browse results live in memory for this long and are
    #: never written to the database.
    header_cache_ttl_seconds: int = Field(default=600, ge=0, le=3600)
    header_cache_max_entries: int = Field(default=256, ge=0)

    # --------------------------------------------------- Microsoft connector
    ms_client_id: str | None = None
    ms_client_secret: SecretStr | None = None
    #: `common` supports the Princeton tenant, other organizations, and personal
    #: Microsoft accounts from one app registration.
    ms_authority: str = "https://login.microsoftonline.com/common"
    ms_scopes: str = "offline_access User.Read Mail.Read"

    # ------------------------------------------------------- Google connector
    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_scopes: str = "https://www.googleapis.com/auth/gmail.readonly"
    #: Hint shown on the Workspace consent screen.
    google_hosted_domain: str | None = "princeton.edu"

    #: Which non-CASA posture the live OAuth client is in.
    #: internal  — GCP project inside the princeton.edu Cloud Organization
    #: trusted   — External, but marked Trusted by the Workspace admin
    #: verified  — External and fully verified (brand + annual CASA)
    #: testing   — External/Testing. 100-user cap, 7-day refresh expiry.
    #: unknown   — not yet determined; `doctor` will say so
    google_oauth_publishing_status: str = "unknown"

    #: Personal Gmail over OAuth is off by default: it is the only path that
    #: requires verification plus an annual CASA assessment.
    enable_personal_gmail_oauth: bool = False

    # --------------------------------------------------------- IMAP connector
    #: The supported path for personal Gmail. App password, EXAMINE-only.
    enable_imap: bool = True
    imap_default_host: str = "imap.gmail.com"
    imap_default_port: int = 993

    # ------------------------------------------------------------- rendering
    #: WeasyPrint first; Chromium is the fallback for layouts it cannot handle.
    enable_playwright_fallback: bool = True
    #: Remote images leak read receipts and break offline rendering.
    allow_remote_images: bool = False
    max_attachment_mb: int = Field(default=25, ge=1, le=150)

    # -------------------------------------------------------------- validators
    @field_validator("required_email_domain")
    @classmethod
    def _normalise_domain(cls, value: str) -> str:
        return value.strip().lstrip("@").lower()

    @field_validator("google_oauth_publishing_status")
    @classmethod
    def _known_posture(cls, value: str) -> str:
        value = value.strip().lower()
        known = {"internal", "trusted", "verified", "testing", "unknown"}
        if value not in known:
            raise ValueError(f"google_oauth_publishing_status must be one of {sorted(known)}")
        return value

    @model_validator(mode="after")
    def _refuse_unsafe_combinations(self) -> Settings:
        # A development bypass that survives to production is not a bypass, it is
        # an unauthenticated endpoint. WEBSITE_SITE_NAME is set by App Service.
        if self.dev_principal and os.environ.get("WEBSITE_SITE_NAME"):
            raise ConfigError(
                "CCREPORT_DEV_PRINCIPAL is set while running on Azure App Service "
                "(WEBSITE_SITE_NAME is present). This would bypass Entra "
                "authentication for every request. Unset it and redeploy."
            )

        if self.dev_encryption_key and os.environ.get("WEBSITE_SITE_NAME"):
            raise ConfigError(
                "CCREPORT_DEV_ENCRYPTION_KEY is set while running on Azure App "
                "Service. Refresh tokens must be wrapped by Key Vault in a "
                "deployed environment; set CCREPORT_KEYVAULT_URL instead."
            )

        # Without a session secret nothing can be signed: OAuth state becomes an
        # unauthenticated parameter and the web forms lose their CSRF token. Both
        # fail open rather than loudly, which is the wrong way round for a
        # deployment, so refuse to boot instead.
        if not self.session_secret and (
            os.environ.get("WEBSITE_SITE_NAME") or self.environment.strip().lower() in {"production", "prod"}
        ):
            raise ConfigError(
                "CCREPORT_SESSION_SECRET is required in a deployed environment. "
                "Without it, OAuth state cannot be signed and web forms carry no "
                "CSRF token. Set it to 32+ random bytes of hex."
            )

        # gmail.readonly is a restricted scope. An unverified External client
        # caps at 100 users and expires refresh tokens after 7 days, which for
        # personal accounts means faculty re-authorising every week — a failure
        # mode that looks like a bug and gets reported as one.
        if self.enable_personal_gmail_oauth and self.google_oauth_publishing_status != "verified":
            raise ConfigError(
                "CCREPORT_ENABLE_PERSONAL_GMAIL_OAUTH is on but "
                f"CCREPORT_GOOGLE_OAUTH_PUBLISHING_STATUS is "
                f"'{self.google_oauth_publishing_status}'. Personal Gmail over "
                "OAuth needs a fully verified client (brand verification plus an "
                "annual CASA assessment). Use the IMAP app-password path instead, "
                "or set the status to 'verified' once verification is complete."
            )

        return self

    # ---------------------------------------------------------------- helpers
    @property
    def on_azure(self) -> bool:
        return bool(os.environ.get("WEBSITE_SITE_NAME"))

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() in {"production", "prod"}

    @property
    def allowed_principal_list(self) -> list[str]:
        return _split_list(self.allowed_principals)

    @property
    def admin_principal_list(self) -> list[str]:
        return _split_list(self.admin_principals)

    @property
    def ms_scope_list(self) -> list[str]:
        return [s for s in self.ms_scopes.split() if s]

    @property
    def google_scope_list(self) -> list[str]:
        return [s for s in self.google_scopes.replace(",", " ").split() if s]

    @property
    def effective_dev_principal(self) -> str | None:
        """The dev bypass principal, or None when it must not apply."""
        if self.on_azure or self.is_production:
            return None
        return self.dev_principal.strip().lower() if self.dev_principal else None

    @property
    def graph_configured(self) -> bool:
        return bool(self.ms_client_id and self.ms_client_secret)

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def google_posture_is_durable(self) -> bool:
        """True when refresh tokens survive longer than seven days.

        'testing' and 'unknown' are not durable: an External/Testing client
        expires every refresh token after 7 days regardless of activity.
        """
        return self.google_oauth_publishing_status in {"internal", "trusted", "verified"}

    def scopes_for(self, provider: str) -> list[str]:
        if provider == "graph":
            return self.ms_scope_list
        if provider == "gmail":
            return self.google_scope_list
        return []


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings. Call ``get_settings.cache_clear()`` in tests."""
    return Settings()
