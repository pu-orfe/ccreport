"""initial schema

The whole retention promise is visible in what this migration does not create:
there is no ``messages`` table and no ``headers`` table. Browsing writes nothing
here, so the only mail-derived rows are the ones a faculty member selected.

Revision ID: 0001
Revises:
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "allowed_principals",
        sa.Column("upn", sa.String(320), primary_key=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("added_by", sa.String(320), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("seeded", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role in ('faculty','admin')", name="ck_allowed_principals_role"),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("upn", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role in ('faculty','admin')", name="ck_users_role"),
    )
    op.create_index("ix_users_upn", "users", ["upn"], unique=True)

    op.create_table(
        "mail_accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("address", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("status_detail", sa.Text(), nullable=True),
        sa.Column("granted_scopes", sa.Text(), nullable=True),
        sa.Column("posture", sa.String(24), nullable=True),
        sa.Column("imap_host", sa.String(255), nullable=True),
        sa.Column("imap_port", sa.Integer(), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "provider", "address", name="uq_mail_accounts_identity"),
        sa.CheckConstraint(
            "provider in ('graph','gmail','imap')", name="ck_mail_accounts_provider"
        ),
        sa.CheckConstraint(
            "status in ('connected','needs_reauth','revoked')", name="ck_mail_accounts_status"
        ),
    )
    op.create_index("ix_mail_accounts_user_id", "mail_accounts", ["user_id"])

    op.create_table(
        "oauth_tokens",
        sa.Column("account_id", sa.String(36), primary_key=True),
        sa.Column("wrapped_dek", sa.LargeBinary(), nullable=False),
        sa.Column("key_name", sa.String(128), nullable=False),
        sa.Column("key_version", sa.String(64), nullable=True),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("access_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["mail_accounts.id"], ondelete="CASCADE"),
    )

    op.create_table(
        "reports",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("period_year", sa.Integer(), nullable=False),
        sa.Column("period_month", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint("status in ('draft','submitted')", name="ck_reports_status"),
        sa.CheckConstraint("period_month between 1 and 12", name="ck_reports_period_month"),
        sa.UniqueConstraint(
            "user_id", "period_year", "period_month", name="uq_reports_user_period"
        ),
    )
    op.create_index("ix_reports_user_id", "reports", ["user_id"])
    op.create_index("ix_reports_period", "reports", ["period_year", "period_month"])

    op.create_table(
        "report_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("report_id", sa.String(36), nullable=False),
        sa.Column("source_account_id", sa.String(36), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("provider_message_id", sa.String(512), nullable=False),
        sa.Column("source_address", sa.String(320), nullable=True),
        sa.Column("message_subject", sa.Text(), nullable=True),
        sa.Column("message_from", sa.String(320), nullable=True),
        sa.Column("message_from_name", sa.String(256), nullable=True),
        sa.Column("message_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("justification", sa.Text(), nullable=True),
        sa.Column("vendor_hint", sa.String(256), nullable=True),
        sa.Column("amount_hint_cents", sa.BigInteger(), nullable=True),
        sa.Column("currency_hint", sa.String(8), nullable=True),
        sa.Column("receipt_score", sa.Integer(), nullable=False),
        sa.Column("artifact_kind", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["report_id"], ["reports.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_account_id"], ["mail_accounts.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("report_id", "provider_message_id", name="uq_report_items_message"),
        sa.CheckConstraint(
            "artifact_kind in ('attachment','message_pdf')", name="ck_report_items_artifact_kind"
        ),
    )
    op.create_index("ix_report_items_report_id", "report_items", ["report_id"])

    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("item_id", sa.String(36), nullable=False),
        sa.Column("blob_path", sa.String(1024), nullable=False),
        sa.Column("filename", sa.String(256), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("renderer", sa.String(24), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["report_items.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_artifacts_item_id", "artifacts", ["item_id"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_upn", sa.String(320), nullable=True),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("subject_type", sa.String(64), nullable=True),
        sa.Column("subject_id", sa.String(128), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"])
    op.create_index("ix_audit_log_actor_upn", "audit_log", ["actor_upn"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_actor_upn", table_name="audit_log")
    op.drop_index("ix_audit_log_at", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_artifacts_item_id", table_name="artifacts")
    op.drop_table("artifacts")
    op.drop_index("ix_report_items_report_id", table_name="report_items")
    op.drop_table("report_items")
    op.drop_index("ix_reports_period", table_name="reports")
    op.drop_index("ix_reports_user_id", table_name="reports")
    op.drop_table("reports")
    op.drop_table("oauth_tokens")
    op.drop_index("ix_mail_accounts_user_id", table_name="mail_accounts")
    op.drop_table("mail_accounts")
    op.drop_index("ix_users_upn", table_name="users")
    op.drop_table("users")
    op.drop_table("allowed_principals")
