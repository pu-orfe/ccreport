from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session
from tests.fakes.connector import PDF_REF, FakeConnector, header

from ccreport.browse import browse, find_header, header_summary, list_folders
from ccreport.cache import HeaderCache
from ccreport.models import MailAccount, User
from ccreport.period import InvalidPeriod
from ccreport.settings import Settings

NOW = _dt.datetime(2026, 8, 15, tzinfo=_dt.UTC)


@pytest.fixture
def account(db_session: Session, faculty: User) -> MailAccount:
    account = MailAccount(
        user_id=faculty.id, provider="graph", address="ada@princeton.edu", status="connected"
    )
    db_session.add(account)
    db_session.flush()
    return account


def connector() -> FakeConnector:
    return FakeConnector(
        [
            header("m1", "Your Amazon.com order receipt", day=3, attachments=(PDF_REF,)),
            header("m2", "Faculty meeting Thursday", day=9, snippet="agenda attached"),
            header("m3", "Invoice 4471 — $120.00", day=17),
            header("old", "June receipt", month=6, day=30),
        ]
    )


def cache() -> HeaderCache:
    return HeaderCache(ttl_seconds=600, max_entries=8)


def test_browsing_scores_and_orders_a_month(db_session, faculty, account, settings) -> None:
    result = browse(
        db_session, faculty, account, "2026-07",
        connector=connector(), settings=settings, cache=cache(), receipts_only=False, now=NOW,
    )
    assert [h.id for h in result.headers] == ["m3", "m2", "m1"]  # newest first
    assert result.total == 3  # the June message is outside the month
    assert result.likely_receipts == 2


def test_highlighting_narrows_the_view_without_hiding_the_month(
    db_session, faculty, account, settings
) -> None:
    shared = cache()
    everything = browse(
        db_session, faculty, account, "2026-07",
        connector=connector(), settings=settings, cache=shared, receipts_only=False, now=NOW,
    )
    receipts = browse(
        db_session, faculty, account, "2026-07",
        connector=connector(), settings=settings, cache=shared, receipts_only=True, now=NOW,
    )
    assert len(everything.headers) == 3
    assert [h.id for h in receipts.headers] == ["m3", "m1"]
    assert receipts.total == 3  # the total still counts everything present


def test_the_second_browse_is_served_from_cache(db_session, faculty, account, settings) -> None:
    shared = cache()
    fake = connector()
    browse(db_session, faculty, account, "2026-07", connector=fake, settings=settings, cache=shared, now=NOW)
    second = browse(
        db_session, faculty, account, "2026-07", connector=fake, settings=settings, cache=shared, now=NOW
    )
    assert len(fake.searches) == 1
    assert second.from_cache


def test_browsing_writes_nothing_durable(db_session, faculty, account, settings) -> None:
    """The retention promise, asserted rather than described."""
    statements: list[str] = []

    @event.listens_for(db_session.get_bind(), "before_cursor_execute")
    def record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(statement.strip().split()[0].upper())

    browse(
        db_session, faculty, account, "2026-07",
        connector=connector(), settings=settings, cache=cache(), now=NOW,
    )
    db_session.flush()
    assert not [s for s in statements if s in {"INSERT", "UPDATE", "DELETE"}]


def test_a_month_outside_the_window_is_refused_not_silently_moved(
    db_session, faculty, account, settings
) -> None:
    with pytest.raises(InvalidPeriod, match="outside the permitted window"):
        browse(
            db_session, faculty, account, "2024-01",
            connector=connector(), settings=settings, cache=cache(), now=NOW,
        )


def test_search_terms_are_applied_even_when_the_provider_ignored_them(
    db_session, faculty, account, settings
) -> None:
    """Graph cannot combine $search with a date filter, so we filter again here."""
    result = browse(
        db_session, faculty, account, "2026-07",
        connector=connector(), settings=settings, cache=cache(),
        subject_contains="invoice", receipts_only=False, now=NOW,
    )
    assert [h.id for h in result.headers] == ["m3"]


def test_filters_are_part_of_the_cache_key(db_session, faculty, account, settings) -> None:
    shared = cache()
    fake = connector()
    browse(db_session, faculty, account, "2026-07", connector=fake, settings=settings, cache=shared, now=NOW)
    browse(
        db_session, faculty, account, "2026-07", connector=fake, settings=settings, cache=shared,
        subject_contains="invoice", now=NOW,
    )
    assert len(fake.searches) == 2


def test_find_header_locates_a_browsed_message(db_session, faculty, account, settings) -> None:
    found = find_header(
        db_session, faculty, account, "2026-07", "m1",
        connector=connector(), settings=settings, cache=cache(), now=NOW,
    )
    assert found is not None and found.subject.startswith("Your Amazon")
    assert (
        find_header(
            db_session, faculty, account, "2026-07", "absent",
            connector=connector(), settings=settings, cache=cache(), now=NOW,
        )
        is None
    )


def test_header_summary_exposes_the_signals_that_justify_a_highlight() -> None:
    from ccreport.filters import score_message

    summary = header_summary(score_message(header("m1", "Receipt for $42.50", attachments=(PDF_REF,))))
    assert summary["likely_receipt"] is True
    assert summary["amount_hint_cents"] == 4250
    assert "receipt-media" in summary["signals"]
    assert summary["attachments"][0]["receipt_media"] is True


def test_folder_listing_is_flattened_for_the_view() -> None:
    folders = list_folders(FakeConnector())
    assert [f["id"] for f in folders] == ["INBOX", "ARCHIVE"]
    assert folders[0]["well_known"] is True


def test_the_window_is_enforced_server_side_not_only_in_the_picker(
    db_session, faculty, account
) -> None:
    narrow = Settings(month_window=1, environment="test")
    with pytest.raises(InvalidPeriod):
        browse(
            db_session, faculty, account, "2026-07",
            connector=connector(), settings=narrow, cache=cache(), now=NOW,
        )
