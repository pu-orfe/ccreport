from __future__ import annotations

from urllib.parse import parse_qs

import pytest
from tests.fakes.gmail_server import FakeGmail

from ccreport.connectors.base import MessageQuery
from ccreport.connectors.gmail import GmailConnector
from ccreport.errors import ReauthRequired
from ccreport.period import month_bounds


def query() -> MessageQuery:
    start, end = month_bounds(2026, 7)
    return MessageQuery(start=start, end=end, folder_ids=("INBOX",), subject_contains="Bob's receipt", from_contains="sales@example.com", has_attachments_only=True, limit=3)


def test_label_listing_with_counts() -> None:
    fake = FakeGmail()
    folders = GmailConnector("token", http=fake.client()).folders()
    assert [(f.id, f.total_count) for f in folders] == [("INBOX", 2), ("Label_1", 1)]
    assert folders[0].well_known
    assert not folders[1].well_known


def test_query_uses_exclusive_before_boundary() -> None:
    fake = FakeGmail()
    messages = GmailConnector("token", http=fake.client()).search(query())
    assert [m.id for m in messages] == ["gm1", "gm2"]
    req = next(r for r in fake.requests if r.url.path.endswith("/messages") and "q=" in str(r.url))
    q = parse_qs(req.url.query.decode())["q"][0]
    assert q == 'after:2026/07/01 before:2026/08/01 has:attachment subject:"Bob\'s receipt" from:sales@example.com label:INBOX'


def test_metadata_attachment_download_and_raw_mime() -> None:
    fake = FakeGmail()
    connector = GmailConnector("token", http=fake.client())
    header = connector.search(query())[0]
    assert header.from_name == "Vendor"
    assert header.from_address == "sales@example.com"
    assert [r.filename for r in header.attachment_refs] == ["receipt.pdf", "pixel.png"]
    assert header.attachment_refs[1].inline
    assert connector.fetch_attachment("gm1", "att1").content == b"pdf-bytes"
    assert b"Subject: Receipt" in (connector.fetch_body("gm1").mime or b"")


def test_status_warns_for_testing_posture() -> None:
    fake = FakeGmail()
    status = GmailConnector("token", http=fake.client(), posture="testing").status()
    assert status.ok
    assert "7 days" in status.warnings[0]


def test_401_raises_reauth_required() -> None:
    fake = FakeGmail()
    fake.fail_auth = True
    with pytest.raises(ReauthRequired):
        GmailConnector("token", http=fake.client()).status()


def test_rate_limit_retries_then_succeeds() -> None:
    fake = FakeGmail()
    fake.rate_once = True
    assert GmailConnector("token", http=fake.client()).status().ok
    assert len(fake.requests) == 2
