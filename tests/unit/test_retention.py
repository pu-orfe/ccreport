"""The retention promise, expressed as tests rather than prose.

"Nothing from a mailbox reaches durable storage unless a faculty member selected
it for a report" is the claim the README makes. These are the checks that make it
false the moment somebody adds a table to hold browse results.
"""

from __future__ import annotations

from ccreport.models import Base, ReportItem


def test_the_schema_has_nowhere_to_put_unselected_mail() -> None:
    tables = set(Base.metadata.tables)
    assert tables == {
        "allowed_principals",
        "artifacts",
        "audit_log",
        "mail_accounts",
        "oauth_tokens",
        "report_items",
        "reports",
        "users",
    }
    assert not [t for t in tables if "message" in t or "header" in t or "cache" in t]


def test_a_selected_item_stores_metadata_but_never_the_body() -> None:
    columns = set(ReportItem.__table__.columns.keys())
    assert {"message_subject", "message_from", "message_date"} <= columns
    assert not [c for c in columns if "body" in c or "html" in c or "raw" in c or "mime" in c]


def test_credentials_are_stored_only_as_ciphertext() -> None:
    from ccreport.models import OAuthToken

    columns = OAuthToken.__table__.columns
    assert {"wrapped_dek", "nonce", "ciphertext"} <= set(columns.keys())
    # No column exists that could hold a token in the clear.
    assert not [
        name
        for name in columns.keys()
        if name in {"refresh_token", "access_token", "password", "app_password", "secret"}
    ]


def test_the_audit_log_is_append_only_by_construction() -> None:
    from ccreport.models import AuditLog

    # No updated_at: nothing in the application rewrites an audit row.
    assert "updated_at" not in AuditLog.__table__.columns
