"""The migration is the schema, and this is where that is proved."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ccreport.models import AllowedPrincipal, Base, MailAccount, User


def test_migrations_produce_exactly_the_orm_schema(migrated_engine: Engine) -> None:
    """Autogenerate finds nothing to do, or the migration has drifted from the models."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    with migrated_engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        differences = compare_metadata(context, Base.metadata)

    assert differences == [], f"schema drift between models and migrations: {differences}"


def test_every_expected_table_exists_and_nothing_else(migrated_engine: Engine) -> None:
    tables = set(inspect(migrated_engine).get_table_names())
    assert tables == set(Base.metadata.tables) | {"alembic_version"}
    assert not [t for t in tables if "message" in t or "header" in t]


def test_downgrade_then_upgrade_returns_to_the_same_schema(
    migrated_engine: Engine, alembic_config
) -> None:
    from alembic import command

    config = alembic_config
    command.downgrade(config, "base")
    assert set(inspect(migrated_engine).get_table_names()) == {"alembic_version"}

    command.upgrade(config, "head")
    assert set(inspect(migrated_engine).get_table_names()) == set(Base.metadata.tables) | {
        "alembic_version"
    }


def test_a_mailbox_cannot_be_connected_twice(pg_session: Session) -> None:
    user = User(upn="ada@princeton.edu", role="faculty")
    pg_session.add(user)
    pg_session.flush()
    for _ in range(2):
        pg_session.add(
            MailAccount(user_id=user.id, provider="graph", address="ada@princeton.edu")
        )
    with pytest.raises(IntegrityError):
        pg_session.flush()


def test_the_role_check_constraint_is_enforced_by_the_database(pg_session: Session) -> None:
    pg_session.add(AllowedPrincipal(upn="ada@princeton.edu", role="superuser"))
    with pytest.raises(IntegrityError):
        pg_session.flush()


def test_deleting_a_user_removes_their_accounts_and_reports(pg_session: Session) -> None:
    from ccreport.models import Report

    user = User(upn="ada@princeton.edu", role="faculty")
    pg_session.add(user)
    pg_session.flush()
    pg_session.add(MailAccount(user_id=user.id, provider="graph", address="ada@princeton.edu"))
    pg_session.add(Report(user_id=user.id, period_year=2026, period_month=7, title="July"))
    pg_session.flush()

    pg_session.execute(text("delete from users where id = :id"), {"id": user.id})
    pg_session.flush()

    assert pg_session.execute(text("select count(*) from mail_accounts")).scalar() == 0
    assert pg_session.execute(text("select count(*) from reports")).scalar() == 0


def test_a_submitted_item_survives_the_mailbox_being_disconnected(pg_session: Session) -> None:
    """``ON DELETE SET NULL`` is what lets history outlive a revoked connection."""
    from ccreport.models import Report, ReportItem

    user = User(upn="ada@princeton.edu", role="faculty")
    pg_session.add(user)
    pg_session.flush()
    account = MailAccount(user_id=user.id, provider="graph", address="ada@princeton.edu")
    report = Report(user_id=user.id, period_year=2026, period_month=7, title="July")
    pg_session.add_all([account, report])
    pg_session.flush()
    item = ReportItem(
        report_id=report.id,
        source_account_id=account.id,
        provider="graph",
        provider_message_id="m1",
    )
    pg_session.add(item)
    pg_session.flush()

    pg_session.execute(text("delete from mail_accounts where id = :id"), {"id": account.id})
    pg_session.expire_all()

    surviving = pg_session.get(ReportItem, item.id)
    assert surviving is not None
    assert surviving.source_account_id is None


def test_timestamps_keep_their_timezone_through_postgres(pg_session: Session) -> None:
    import datetime as _dt

    user = User(upn="ada@princeton.edu", role="faculty")
    pg_session.add(user)
    pg_session.flush()
    pg_session.expire_all()

    stored = pg_session.get(User, user.id)
    assert stored.created_at.tzinfo is not None
    assert abs((stored.created_at - _dt.datetime.now(_dt.UTC)).total_seconds()) < 60
