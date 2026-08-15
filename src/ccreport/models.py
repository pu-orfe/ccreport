"""Database models.

The schema encodes the retention promise. There is no ``messages`` table and no
``headers`` table: nothing from a mailbox reaches durable storage unless a
faculty member selected it for a report, at which point it becomes a
``ReportItem`` with an ``Artifact``. Browse results live in an in-process cache
with a TTL and are never written here.

``oauth_tokens`` stores ciphertext only. The wrapping key lives in Key Vault and
the column carries the key version used, so rotation is a re-wrap rather than a
forced reconnect for every user.
"""

from __future__ import annotations

import datetime as _dt
import uuid

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


class Base(DeclarativeBase):
    pass


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC)


def _uuid() -> str:
    return str(uuid.uuid4())


class TimestampMixin:
    created_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


class AllowedPrincipal(TimestampMixin, Base):
    """The access list. Absence from this table is a denial.

    Kept separate from :class:`User` so that a principal can be authorised
    before they have ever signed in, which is how an administrator onboards
    somebody without waiting for them.
    """

    __tablename__ = "allowed_principals"

    upn: Mapped[str] = mapped_column(String(320), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default="faculty", nullable=False)
    added_by: Mapped[str | None] = mapped_column(String(320), default=None)
    note: Mapped[str | None] = mapped_column(Text, default=None)
    #: Seeded from settings rather than added by a person. Kept so a redeploy
    #: can reconcile the seed without clobbering manual grants.
    seeded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    __table_args__ = (
        CheckConstraint("role in ('faculty','admin')", name="ck_allowed_principals_role"),
    )


class User(TimestampMixin, Base):
    """Somebody who has actually signed in.

    Created on first authenticated request, never by an administrator. The
    authoritative grant is :class:`AllowedPrincipal`; this row is a record of
    use, and its ``role`` is a cache of the grant for cheap joins.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    upn: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(256), default=None)
    role: Mapped[str] = mapped_column(String(16), default="faculty", nullable=False)
    last_seen_at: Mapped[_dt.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    accounts: Mapped[list[MailAccount]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    reports: Mapped[list[Report]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (CheckConstraint("role in ('faculty','admin')", name="ck_users_role"),)


class MailAccount(TimestampMixin, Base):
    """One connected mailbox belonging to one user.

    A user may connect several: institutional Outlook, institutional Workspace,
    and a personal account over IMAP are all ordinary here.
    """

    __tablename__ = "mail_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    address: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(256), default=None)

    #: connected | needs_reauth | revoked
    status: Mapped[str] = mapped_column(String(24), default="connected", nullable=False)
    status_detail: Mapped[str | None] = mapped_column(Text, default=None)
    granted_scopes: Mapped[str | None] = mapped_column(Text, default=None)

    #: For Google, which consent posture was in force when this was connected.
    #: A 'testing' connection will die after seven days and we should say so.
    posture: Mapped[str | None] = mapped_column(String(24), default=None)

    connected_at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    last_verified_at: Mapped[_dt.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    revoked_at: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    user: Mapped[User] = relationship(back_populates="accounts")
    token: Mapped[OAuthToken | None] = relationship(
        back_populates="account", cascade="all, delete-orphan", uselist=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "provider", "address", name="uq_mail_accounts_identity"),
        CheckConstraint("provider in ('graph','gmail','imap')", name="ck_mail_accounts_provider"),
        CheckConstraint(
            "status in ('connected','needs_reauth','revoked')", name="ck_mail_accounts_status"
        ),
    )


class OAuthToken(TimestampMixin, Base):
    """Envelope-encrypted credential for one account.

    Holds an OAuth refresh token or an IMAP app password; both are long-lived
    bearer secrets and neither is ever stored in the clear. ``key_version``
    records which Key Vault key version wrapped ``wrapped_dek``, so rotating the
    key is a background re-wrap and not a mass reconnect.
    """

    __tablename__ = "oauth_tokens"

    account_id: Mapped[str] = mapped_column(
        ForeignKey("mail_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    #: The data key, wrapped by the Key Vault key.
    wrapped_dek: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_name: Mapped[str] = mapped_column(String(128), nullable=False)
    key_version: Mapped[str | None] = mapped_column(String(64), default=None)
    #: AES-GCM nonce and ciphertext of the secret material.
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    #: Short-lived access tokens are cached in memory, not here; only the expiry
    #: is persisted so the UI can say when a silent refresh is due.
    access_expires_at: Mapped[_dt.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    refreshed_at: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    account: Mapped[MailAccount] = relationship(back_populates="token")


class Report(TimestampMixin, Base):
    """One month's collection of receipts for one faculty member."""

    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    period_year: Mapped[int] = mapped_column(Integer, nullable=False)
    period_month: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)

    #: draft | submitted
    status: Mapped[str] = mapped_column(String(16), default="draft", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, default=None)

    submitted_at: Mapped[_dt.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    #: Reserved for the v2 notification and nudge work; nullable so adding it
    #: later is not a migration fight.
    notified_at: Mapped[_dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    user: Mapped[User] = relationship(back_populates="reports")
    items: Mapped[list[ReportItem]] = relationship(
        back_populates="report", cascade="all, delete-orphan", order_by="ReportItem.position"
    )

    __table_args__ = (
        CheckConstraint("status in ('draft','submitted')", name="ck_reports_status"),
        CheckConstraint("period_month between 1 and 12", name="ck_reports_period_month"),
        Index("ix_reports_period", "period_year", "period_month"),
    )

    @property
    def period(self) -> str:
        return f"{self.period_year:04d}-{self.period_month:02d}"


class ReportItem(TimestampMixin, Base):
    """One selected message, its justification, and what it became.

    The message metadata copied here is the minimum needed to identify the
    charge on a Concur line — sender, subject, date, and the extracted vendor and
    amount. The message body is not copied; it is either an attachment or a
    rendered PDF in :class:`Artifact`.
    """

    __tablename__ = "report_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    report_id: Mapped[str] = mapped_column(
        ForeignKey("reports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Nullable: an account may be disconnected after a report is submitted, and
    #: the submitted report must survive that.
    source_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("mail_accounts.id", ondelete="SET NULL"), default=None
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_message_id: Mapped[str] = mapped_column(String(512), nullable=False)
    source_address: Mapped[str | None] = mapped_column(String(320), default=None)

    message_subject: Mapped[str | None] = mapped_column(Text, default=None)
    message_from: Mapped[str | None] = mapped_column(String(320), default=None)
    message_from_name: Mapped[str | None] = mapped_column(String(256), default=None)
    message_date: Mapped[_dt.datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    justification: Mapped[str | None] = mapped_column(Text, default=None)
    vendor_hint: Mapped[str | None] = mapped_column(String(256), default=None)
    amount_hint_cents: Mapped[int | None] = mapped_column(BigInteger, default=None)
    currency_hint: Mapped[str | None] = mapped_column(String(8), default=None)
    receipt_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: attachment | message_pdf — what ends up in the bundle for this item.
    artifact_kind: Mapped[str] = mapped_column(String(16), default="message_pdf", nullable=False)

    report: Mapped[Report] = relationship(back_populates="items")
    artifacts: Mapped[list[Artifact]] = relationship(
        back_populates="item", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("report_id", "provider_message_id", name="uq_report_items_message"),
        CheckConstraint(
            "artifact_kind in ('attachment','message_pdf')", name="ck_report_items_artifact_kind"
        ),
    )


class Artifact(TimestampMixin, Base):
    """A stored file: an original attachment or a rendered message PDF."""

    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    item_id: Mapped[str] = mapped_column(
        ForeignKey("report_items.id", ondelete="CASCADE"), nullable=False, index=True
    )

    #: Path within the blob container, or relative to the local artifact dir.
    blob_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer, default=None)

    #: original_attachment | rendered_message | converted_image
    kind: Mapped[str] = mapped_column(String(24), default="original_attachment", nullable=False)
    #: weasyprint | playwright | none — kept because a rendering regression is
    #: otherwise impossible to attribute after the fact.
    renderer: Mapped[str | None] = mapped_column(String(24), default=None)

    item: Mapped[ReportItem] = relationship(back_populates="artifacts")


class AuditLog(Base):
    """Who did what. Append-only; nothing in the application updates a row."""

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    at: Mapped[_dt.datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )
    actor_upn: Mapped[str | None] = mapped_column(String(320), default=None, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_type: Mapped[str | None] = mapped_column(String(64), default=None)
    subject_id: Mapped[str | None] = mapped_column(String(128), default=None)
    detail: Mapped[dict | None] = mapped_column(JSON, default=None)


__all__ = [
    "AllowedPrincipal",
    "Artifact",
    "AuditLog",
    "Base",
    "MailAccount",
    "OAuthToken",
    "Report",
    "ReportItem",
    "User",
]
