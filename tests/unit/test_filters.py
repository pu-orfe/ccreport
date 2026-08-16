from __future__ import annotations

from pathlib import Path

import pytest

from ccreport.connectors.base import AttachmentRef, MessageHeader
from ccreport.filters import (
    RECEIPT_THRESHOLD,
    extract_amount,
    extract_vendor,
    header_matches,
    load_vendors,
    score_message,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Total $1,234.56", (123456, "USD")),
        ("Total USD 1,234.56", (123456, "USD")),
        ("Gesamt 1.234,56 €", (123456, "EUR")),
        ("Total £99", (9900, "GBP")),
        ("Total ¥1200", (1200, "JPY")),
        ("Total $1,234.56 USD", (123456, "USD")),
        ("Refund -$50.00", (-5000, "USD")),
        ("Refund ($50.00)", (-5000, "USD")),
        ("Subtotal $10.00 tax $1.00 total $25.00", (2500, "USD")),
    ],
)
def test_extract_amount_formats(text: str, expected: tuple[int, str]) -> None:
    assert extract_amount(text) == expected


@pytest.mark.parametrize("text", ["2026", "order 123456", "tracking 999999999999", "Save 15%"])
def test_extract_amount_rejects_non_amounts(text: str) -> None:
    assert extract_amount(text) is None


def test_extract_vendor_prefers_known_vendor() -> None:
    header = MessageHeader(id="1", from_name="Amazon Receipts", from_address="auto@email.amazon.com")
    assert extract_vendor(header) == "Amazon"


def test_extract_vendor_cleans_sender_name() -> None:
    header = MessageHeader(id="1", from_name="Acme Billing Team via Mailer", from_address="x@y.com")
    assert extract_vendor(header) == "Acme"


def test_extract_vendor_falls_back_to_domain() -> None:
    header = MessageHeader(id="1", from_address="no-reply@notifications.fancy-shop.com")
    assert extract_vendor(header) == "Fancy Shop"


def test_score_message_full_matrix_and_signals() -> None:
    header = MessageHeader(
        id="1",
        subject="Order confirmation and receipt",
        from_address="receipts@amazon.com",
        snippet="Total $42.00",
        attachment_refs=[AttachmentRef("a", "receipt.pdf", "application/pdf", 10)],
    )
    scored = score_message(header)
    assert scored.receipt_score >= 11
    assert scored.likely_receipt
    assert scored.amount_hint_cents == 4200
    assert scored.currency_hint == "USD"
    assert scored.vendor_hint == "Amazon"
    assert "receipt-media" in scored.matched_signals
    assert "keyword:receipt" in scored.matched_signals
    assert "vendor:Amazon" in scored.matched_signals
    assert "amount:USD" in scored.matched_signals


def test_marketing_email_with_amount_scores_below_threshold() -> None:
    header = MessageHeader(
        id="1",
        subject="Newsletter",
        from_address="newsletter@example.com",
        snippet="Deal of the week $99.00 unsubscribe",
    )
    scored = score_message(header)
    assert scored.receipt_score < RECEIPT_THRESHOLD
    assert not scored.likely_receipt
    assert "marketing" in scored.matched_signals


def test_plain_pdf_attachment_unknown_sender_reaches_threshold() -> None:
    header = MessageHeader(
        id="1",
        from_address="person@unknown.example",
        attachment_refs=[AttachmentRef("a", "scan.pdf", "application/pdf", 10)],
    )
    scored = score_message(header)
    assert scored.receipt_score >= RECEIPT_THRESHOLD
    assert scored.likely_receipt


def test_calendar_negative_signal() -> None:
    header = MessageHeader(id="1", subject="Invitation: lab meeting", snippet="confirmation $10.00")
    scored = score_message(header)
    assert "calendar" in scored.matched_signals
    assert scored.receipt_score == 2


def test_load_vendors_json_and_lines() -> None:
    data = Path("tests/data")
    assert load_vendors(data / "vendors.json") == {
        "example.edu": "Example Vendor",
        "customshop": "Custom Shop",
    }
    assert load_vendors(data / "vendors.txt") == {
        "foo.example": "Foo Example",
        "bar shop": "Bar Shop",
        "baz.example": "Baz",
    }


def test_score_message_uses_loaded_vendors() -> None:
    header = MessageHeader(id="1", from_address="billing@example.edu")
    scored = score_message(header, vendors=load_vendors("tests/data/vendors.json"))
    assert scored.receipt_score == 2
    assert scored.vendor_hint == "Example Vendor"
    assert not scored.likely_receipt


def test_header_matches() -> None:
    header = MessageHeader(
        id="1",
        subject="Your Receipt",
        from_name="Store Team",
        from_address="receipts@store.example",
    )
    assert header_matches(header, subject_contains="receipt", from_contains="STORE")
    assert not header_matches(header, subject_contains="invoice")
    assert not header_matches(header, from_contains="other")
