from __future__ import annotations

import pytest
from tests.fakes.imap_server import FakeImap

from ccreport.connectors.base import MessageQuery
from ccreport.connectors.imap import ImapConnector, decode_modified_utf7
from ccreport.errors import ReauthRequired
from ccreport.period import month_bounds


def make_connector(fake: FakeImap) -> ImapConnector:
    return ImapConnector("imap.example.com", 993, "ada@example.edu", "app-password", imap_factory=lambda *a, **k: fake)


def query() -> MessageQuery:
    start, end = month_bounds(2026, 7)
    return MessageQuery(start=start, end=end, subject_contains="receipt", from_contains="vendor", limit=2)


def test_folders_decode_modified_utf7_and_spaces() -> None:
    fake = FakeImap()
    folders = make_connector(fake).folders()
    assert [f.display_path for f in folders] == ["INBOX", "Travel 日本語", "Receipts 2026"]
    assert decode_modified_utf7("A &- B") == "A & B"


def test_search_uses_date_range_headers_and_body_peek() -> None:
    fake = FakeImap()
    messages = make_connector(fake).search(query())
    assert messages[0].id == "777:101"
    search = next(c for c in fake.commands if c.startswith("UID SEARCH"))
    assert "SINCE 01-Jul-2026 BEFORE 01-Aug-2026" in search
    assert 'HEADER SUBJECT "receipt"' in search
    fetches = [c for c in fake.commands if c.startswith("UID FETCH")]
    assert any("BODY.PEEK[HEADER.FIELDS" in c for c in fetches)
    assert any("BODY.PEEK[]" in c for c in fetches)
    assert all(" BODY[" not in c for c in fetches)


def test_header_decoding_attachment_download_and_raw_mime() -> None:
    fake = FakeImap()
    connector = make_connector(fake)
    messages = connector.search(query())
    assert messages[0].subject == "PDF receipt"
    assert messages[0].attachment_refs[0].filename == "receipt.pdf"
    assert messages[1].subject == "Recépisse"
    attachment = connector.fetch_attachment(messages[0].id, messages[0].attachment_refs[0].id)
    assert attachment.ref.filename == "receipt.pdf"
    assert attachment.content.startswith(b"%PDF")
    assert b"PDF receipt" in (connector.fetch_body(messages[0].id).mime or b"")


def test_imap_read_only_enforcement_records_no_mutating_verbs() -> None:
    fake = FakeImap()
    connector = make_connector(fake)
    connector.status()
    connector.search(query())
    connector.fetch_body("777:101")
    mutating = ("SELECT", "STORE", "APPEND", "EXPUNGE", "COPY", "MOVE")
    assert not any(c.split()[0] in mutating for c in fake.commands)
    assert any(c.startswith("EXAMINE") for c in fake.commands)


def test_authentication_failed_raises_reauth() -> None:
    fake = FakeImap()
    fake.auth_fail = True
    with pytest.raises(ReauthRequired):
        make_connector(fake).status()
