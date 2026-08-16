from __future__ import annotations

import csv
import dataclasses
import datetime as _dt
import io
import json
import zipfile

import pytest
from sqlalchemy.orm import Session
from tests.fakes.connector import PDF_REF, FakeConnector, header

from ccreport.bundle import build_bundle, bundle_filename, format_amount, slugify
from ccreport.filters import score_message
from ccreport.models import MailAccount, User
from ccreport.reports import add_item, create_report, justify_item, submit_report
from ccreport.settings import Settings

NOW = _dt.datetime(2026, 8, 15, tzinfo=_dt.UTC)
PERIOD = "2026-07"


@pytest.fixture
def bundle(db_session: Session, faculty: User, settings: Settings, store) -> zipfile.ZipFile:
    account = MailAccount(
        user_id=faculty.id, provider="graph", address="ada@princeton.edu", status="connected"
    )
    db_session.add(account)
    db_session.flush()

    connector = FakeConnector(
        [
            header("m1", "Amazon.com order receipt for $42.50", day=3, attachments=(PDF_REF,)),
            header(
                "m2", "W.B. Mason invoice — $120.00", day=17,
                attachments=(dataclasses.replace(PDF_REF, filename="../../etc/passwd"),),
            ),
        ],
        attachments={
            ("m1", "att-pdf"): b"%PDF-1.4 amazon\n",
            ("m2", "att-pdf"): b"%PDF-1.4 mason\n",
        },
    )
    report = create_report(db_session, faculty, PERIOD, settings=settings, now=NOW)
    for message_id, justification in [
        ("m1", "Textbooks for ORF 405"),
        ("m2", "Whiteboard markers for the lab"),
    ]:
        item = add_item(
            db_session, faculty, report, account,
            score_message(next(h for h in connector.headers if h.id == message_id)),
        )
        justify_item(db_session, faculty, report, item.id, justification)

    submit_report(
        db_session, faculty, report,
        open_connector=lambda a: connector, store=store, settings=settings,
    )
    content = build_bundle(report, faculty, store, generated_at=NOW)
    return zipfile.ZipFile(io.BytesIO(content))


def manifest(bundle: zipfile.ZipFile) -> dict:
    return json.loads(bundle.read("manifest.json"))


def test_the_bundle_contains_exactly_the_documented_files(bundle: zipfile.ZipFile) -> None:
    names = set(bundle.namelist())
    assert {"manifest.json", "summary.csv", "index.html", "ccworks-apply.json"} <= names
    assert sorted(n for n in names if n.startswith("receipts/")) == [
        "receipts/001-amazon.pdf",
        "receipts/002-w-b-mason.pdf",
    ]


def test_receipt_filenames_are_zero_padded_and_derived_from_the_vendor(bundle) -> None:
    """A hostile attachment name cannot decide where a file lands."""
    assert "receipts/002-w-b-mason.pdf" in bundle.namelist()
    assert not [n for n in bundle.namelist() if ".." in n or n.startswith("/")]


def test_manifest_is_schema_versioned_and_records_every_hash(bundle) -> None:
    import hashlib

    data = manifest(bundle)
    assert data["schema_version"] == 1
    assert data["period"] == PERIOD
    assert data["user"]["upn"] == "ada@princeton.edu"
    assert data["totals"] == {
        "items": 2,
        "artifacts": 2,
        "amount_cents": 16250,
        "amount": "162.50",
        "currency": "USD",
        "currencies": ["USD"],
    }
    first = data["items"][0]["artifacts"][0]
    assert first["sha256"] == hashlib.sha256(bundle.read(first["file"])).hexdigest()


def test_every_item_carries_its_justification_and_source(bundle) -> None:
    items = manifest(bundle)["items"]
    assert [i["justification"] for i in items] == [
        "Textbooks for ORF 405",
        "Whiteboard markers for the lab",
    ]
    assert items[0]["source"] == {
        "provider": "graph",
        "account": "ada@princeton.edu",
        "message_id": "m1",
    }


def test_summary_csv_opens_without_an_import_wizard(bundle) -> None:
    rows = list(csv.reader(io.StringIO(bundle.read("summary.csv").decode())))
    assert rows[0] == ["date", "vendor", "amount", "currency", "justification", "filename"]
    assert rows[1][:4] == ["2026-07-03", "Amazon", "42.50", "USD"]
    assert rows[1][5] == "receipts/001-amazon.pdf"
    assert len(rows) == 3


def test_apply_json_omits_index_and_carries_vendor_and_amount(bundle) -> None:
    """Concur row indices are positional; vendor and amount are what can be matched."""
    payload = json.loads(bundle.read("ccworks-apply.json"))
    assert payload["schema_version"] == 1
    assert payload["period"] == PERIOD
    receipt = payload["receipts"][0]
    assert "index" not in receipt
    assert receipt["vendor"] == "Amazon"
    assert receipt["amount"] == "42.50"
    assert receipt["currency"] == "USD"
    assert receipt["file"] == "receipts/001-amazon.pdf"
    assert receipt["date"] == "2026-07-03"
    assert receipt["sha256"]


def test_contact_sheet_lists_every_receipt(bundle) -> None:
    html = bundle.read("index.html").decode()
    assert "Receipts for July 2026" in html
    assert "Textbooks for ORF 405" in html
    assert "receipts/001-amazon.pdf" in html
    assert "162.50 USD" in html


def test_bundle_filename_uses_the_local_part_and_the_period() -> None:
    assert bundle_filename("ada.lovelace@princeton.edu", "2026-07") == "ccreport-ada-lovelace-2026-07.zip"
    assert bundle_filename("", "2026-07") == "ccreport-user-2026-07.zip"


@pytest.mark.parametrize(
    ("cents", "currency", "expected"),
    [
        (4250, "USD", "42.50"),
        (5, "USD", "0.05"),
        (-1000, "EUR", "-10.00"),
        (1200, "JPY", "1200"),
        (None, "USD", None),
    ],
)
def test_amounts_are_formatted_for_the_currency(cents, currency, expected) -> None:
    assert format_amount(cents, currency) == expected


def test_slugify_is_stable_and_bounded() -> None:
    assert slugify("W.B. Mason — Order #7") == "w-b-mason-order-7"
    assert slugify("   ") == "receipt"
    assert len(slugify("x" * 200)) == 40


def test_mixed_currencies_are_stated_rather_than_summed(
    db_session, faculty, settings, store
) -> None:
    account = MailAccount(
        user_id=faculty.id, provider="graph", address="ada@princeton.edu", status="connected"
    )
    db_session.add(account)
    db_session.flush()
    connector = FakeConnector(
        [
            header("usd", "Receipt $10.00", day=2, attachments=(PDF_REF,), snippet=""),
            header("eur", "Quittung €20,00", day=3, attachments=(PDF_REF,), snippet=""),
        ]
    )
    report = create_report(db_session, faculty, PERIOD, settings=settings, now=NOW)
    for message in connector.headers:
        item = add_item(db_session, faculty, report, account, score_message(message))
        justify_item(db_session, faculty, report, item.id, "Conference travel")
    submit_report(
        db_session, faculty, report,
        open_connector=lambda a: connector, store=store, settings=settings,
    )

    totals = json.loads(
        zipfile.ZipFile(io.BytesIO(build_bundle(report, faculty, store, generated_at=NOW)))
        .read("manifest.json")
    )["totals"]
    assert totals["currency"] is None
    assert totals["currencies"] == ["EUR", "USD"]
