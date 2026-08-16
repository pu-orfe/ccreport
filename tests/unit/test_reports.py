from __future__ import annotations

import datetime as _dt

import pytest
from sqlalchemy.orm import Session
from tests.fakes.connector import PDF_REF, FakeConnector, header, message_bytes

from ccreport.errors import RenderError
from ccreport.filters import score_message
from ccreport.models import Artifact, MailAccount, Report, User
from ccreport.reports import (
    ItemNotFound,
    ReportNotFound,
    ReportStateError,
    add_item,
    admin_list_reports,
    create_report,
    export_bundle,
    get_report,
    justify_item,
    list_reports,
    remove_item,
    report_summary,
    submit_report,
)
from ccreport.settings import Settings

NOW = _dt.datetime(2026, 8, 15, tzinfo=_dt.UTC)
PERIOD = "2026-07"


@pytest.fixture
def account(db_session: Session, faculty: User) -> MailAccount:
    account = MailAccount(
        user_id=faculty.id, provider="graph", address="ada@princeton.edu", status="connected"
    )
    db_session.add(account)
    db_session.flush()
    return account


@pytest.fixture
def connector() -> FakeConnector:
    return FakeConnector(
        [
            header("m1", "Amazon.com order receipt", day=3, attachments=(PDF_REF,)),
            header("m2", "Invoice 4471 for $120.00", day=17),
        ],
        bodies={"m2": message_bytes("Invoice 4471 for $120.00")},
        attachments={("m1", "att-pdf"): b"%PDF-1.4 vendor receipt\n"},
    )


@pytest.fixture
def report(db_session: Session, faculty: User, settings: Settings) -> Report:
    return create_report(db_session, faculty, PERIOD, settings=settings, now=NOW)


def select(db_session, faculty, report, account, connector, message_id, justification=None):
    item = add_item(
        db_session, faculty, report, account,
        score_message(next(h for h in connector.headers if h.id == message_id)),
    )
    if justification:
        justify_item(db_session, faculty, report, item.id, justification)
    return item


# ------------------------------------------------------------------- lifecycle
def test_creating_a_report_is_idempotent_per_month(db_session, faculty, settings) -> None:
    first = create_report(db_session, faculty, PERIOD, settings=settings, now=NOW)
    second = create_report(db_session, faculty, PERIOD, settings=settings, now=NOW)
    assert first.id == second.id
    assert len(list_reports(db_session, faculty)) == 1


def test_a_missing_report_says_how_to_create_one(db_session, faculty) -> None:
    with pytest.raises(ReportNotFound, match="report create --month"):
        get_report(db_session, faculty, "2026-06")


def test_selecting_a_message_copies_the_minimum_needed_to_identify_a_charge(
    db_session, faculty, report, account, connector
) -> None:
    item = select(db_session, faculty, report, account, connector, "m1")
    assert item.provider_message_id == "m1"
    assert item.vendor_hint == "Amazon"
    assert item.message_subject == "Amazon.com order receipt"
    assert item.artifact_kind == "attachment"
    assert item.justification is None


def test_selecting_the_same_message_twice_does_not_duplicate_it(
    db_session, faculty, report, account, connector
) -> None:
    first = select(db_session, faculty, report, account, connector, "m1")
    second = select(db_session, faculty, report, account, connector, "m1")
    assert first.id == second.id
    assert report_summary(report)["items"] == 1


def test_removing_an_item_renumbers_the_rest(db_session, faculty, report, account, connector) -> None:
    select(db_session, faculty, report, account, connector, "m1")
    select(db_session, faculty, report, account, connector, "m2")

    remove_item(db_session, faculty, report, 1)
    summary = report_summary(report)
    assert [i["index"] for i in summary["receipts"]] == [1]
    assert summary["receipts"][0]["message_id"] == "m2"


def test_items_can_be_addressed_by_position_or_id(db_session, faculty, report, account, connector) -> None:
    item = select(db_session, faculty, report, account, connector, "m1")
    justify_item(db_session, faculty, report, 1, "Books for ORF 405")
    assert item.justification == "Books for ORF 405"

    justify_item(db_session, faculty, report, item.id, "Revised")
    assert item.justification == "Revised"

    with pytest.raises(ItemNotFound):
        justify_item(db_session, faculty, report, 9, "nope")


def test_an_empty_justification_is_refused(db_session, faculty, report, account, connector) -> None:
    select(db_session, faculty, report, account, connector, "m1")
    with pytest.raises(ReportStateError, match="cannot be empty"):
        justify_item(db_session, faculty, report, 1, "   ")


# ------------------------------------------------------------------ submission
def test_submission_refuses_an_empty_report(db_session, faculty, report, settings, store) -> None:
    with pytest.raises(ReportStateError, match="no selected receipts"):
        submit_report(
            db_session, faculty, report, open_connector=lambda a: None, store=store, settings=settings
        )


def test_submission_refuses_while_a_justification_is_missing(
    db_session, faculty, report, account, connector, settings, store
) -> None:
    select(db_session, faculty, report, account, connector, "m1")
    select(db_session, faculty, report, account, connector, "m2", "Conference registration")

    with pytest.raises(ReportStateError, match="item\\(s\\) 1 have no justification"):
        submit_report(
            db_session, faculty, report,
            open_connector=lambda a: connector, store=store, settings=settings,
        )
    assert report.status == "draft"


def test_submission_prefers_the_original_attachment_over_a_rendering(
    db_session, faculty, report, account, connector, settings, store
) -> None:
    select(db_session, faculty, report, account, connector, "m1", "Textbooks for ORF 405")
    submit_report(
        db_session, faculty, report,
        open_connector=lambda a: connector, store=store, settings=settings,
    )
    item = report.items[0]
    artifact = item.artifacts[0]

    assert item.artifact_kind == "attachment"
    assert artifact.kind == "original_attachment"
    assert store.get(artifact.blob_path) == b"%PDF-1.4 vendor receipt\n"
    assert connector.fetched_bodies == []  # the body was never fetched


def test_a_message_without_an_attachment_is_rendered_to_pdf(
    db_session, faculty, report, account, connector, settings, store, pdf_renderer
) -> None:
    select(db_session, faculty, report, account, connector, "m2", "Journal subscription")
    submit_report(
        db_session, faculty, report,
        open_connector=lambda a: connector, store=store, settings=settings,
    )
    artifact = report.items[0].artifacts[0]

    assert report.items[0].artifact_kind == "message_pdf"
    assert artifact.kind == "rendered_message"
    assert artifact.content_type == "application/pdf"
    assert store.get(artifact.blob_path).startswith(b"%PDF")
    assert connector.fetched_bodies == ["m2"]


def test_a_submitted_report_is_frozen(
    db_session, faculty, report, account, connector, settings, store
) -> None:
    select(db_session, faculty, report, account, connector, "m1", "Textbooks")
    submit_report(
        db_session, faculty, report,
        open_connector=lambda a: connector, store=store, settings=settings,
    )
    assert report.status == "submitted" and report.submitted_at is not None

    with pytest.raises(ReportStateError, match="can no longer be changed"):
        justify_item(db_session, faculty, report, 1, "second thoughts")
    with pytest.raises(ReportStateError):
        submit_report(
            db_session, faculty, report,
            open_connector=lambda a: connector, store=store, settings=settings,
        )


def test_a_failed_render_leaves_the_report_a_draft_and_no_orphaned_files(
    db_session, faculty, report, account, connector, settings, store, monkeypatch
) -> None:
    select(db_session, faculty, report, account, connector, "m1", "Textbooks")
    select(db_session, faculty, report, account, connector, "m2", "Journal")

    def explode(*args, **kwargs):
        raise RenderError("no renderer could produce a PDF")

    monkeypatch.setattr("ccreport.reports.render_message_to_pdf", explode)

    with pytest.raises(RenderError):
        submit_report(
            db_session, faculty, report,
            open_connector=lambda a: connector, store=store, settings=settings,
        )
    assert report.status == "draft"
    assert store.delete_prefix(f"reports/{report.id}") == 0


def test_an_item_whose_mailbox_was_disconnected_blocks_submission_with_a_reason(
    db_session, faculty, report, account, connector, settings, store
) -> None:
    item = select(db_session, faculty, report, account, connector, "m1", "Textbooks")
    item.source_account_id = None
    db_session.flush()

    with pytest.raises(ReportStateError, match="no longer connected"):
        submit_report(
            db_session, faculty, report,
            open_connector=lambda a: connector, store=store, settings=settings,
        )


def test_an_oversized_attachment_is_skipped_and_the_message_rendered_instead(
    db_session, faculty, report, account, settings, store, pdf_renderer
) -> None:
    huge = FakeConnector(
        [header("big", "Receipt", day=4, attachments=(PDF_REF,))],
        attachments={("big", "att-pdf"): b"x" * (2 * 1024 * 1024)},
    )
    tiny_limit = settings.model_copy(update={"max_attachment_mb": 1})
    add_item(db_session, faculty, report, account, score_message(huge.headers[0]))
    justify_item(db_session, faculty, report, 1, "Equipment")

    submit_report(
        db_session, faculty, report,
        open_connector=lambda a: huge, store=store, settings=tiny_limit,
    )
    assert report.items[0].artifacts[0].kind == "rendered_message"


# ---------------------------------------------------------------------- export
def test_export_produces_a_named_bundle(
    db_session, faculty, report, account, connector, settings, store
) -> None:
    select(db_session, faculty, report, account, connector, "m1", "Textbooks for ORF 405")
    submit_report(
        db_session, faculty, report,
        open_connector=lambda a: connector, store=store, settings=settings,
    )
    filename, content = export_bundle(db_session, report, faculty, store=store, settings=settings)

    assert filename == "ccreport-ada-2026-07.zip"
    assert content.startswith(b"PK")


# ----------------------------------------------------------------------- admin
def test_administrators_see_submitted_reports_but_not_drafts(
    db_session, faculty, admin, report, account, connector, settings, store
) -> None:
    select(db_session, faculty, report, account, connector, "m1", "Textbooks")
    draft = create_report(db_session, faculty, "2026-06", settings=settings, now=NOW)

    assert admin_list_reports(db_session, admin) == []

    submit_report(
        db_session, faculty, report,
        open_connector=lambda a: connector, store=store, settings=settings,
    )
    rows = admin_list_reports(db_session, admin)
    assert [(u.upn, r.period) for u, r in rows] == [(faculty.upn, PERIOD)]
    assert draft.status == "draft"


def test_faculty_cannot_list_everybody_elses_reports(db_session, faculty) -> None:
    from ccreport.auth.allowlist import InsufficientRole

    with pytest.raises(InsufficientRole):
        admin_list_reports(db_session, faculty)


def test_summary_counts_what_still_needs_a_justification(
    db_session, faculty, report, account, connector
) -> None:
    select(db_session, faculty, report, account, connector, "m1")
    select(db_session, faculty, report, account, connector, "m2", "Journal")
    summary = report_summary(report)

    assert summary["items"] == 2
    assert summary["unjustified"] == 1
    assert summary["period_label"] == "July 2026"


def test_artifacts_record_their_own_hash_and_size(
    db_session, faculty, report, account, connector, settings, store
) -> None:
    select(db_session, faculty, report, account, connector, "m1", "Textbooks")
    submit_report(
        db_session, faculty, report,
        open_connector=lambda a: connector, store=store, settings=settings,
    )
    artifact: Artifact = report.items[0].artifacts[0]
    import hashlib

    assert artifact.size_bytes == len(b"%PDF-1.4 vendor receipt\n")
    assert artifact.sha256 == hashlib.sha256(b"%PDF-1.4 vendor receipt\n").hexdigest()
