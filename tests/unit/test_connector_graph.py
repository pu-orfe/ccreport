from __future__ import annotations

from urllib.parse import parse_qs

import pytest
from tests.fakes.graph_server import FakeGraph

from ccreport.connectors.base import MessageQuery
from ccreport.connectors.graph import GraphConnector, _odata_string_literal
from ccreport.errors import ReauthRequired
from ccreport.period import month_bounds


def query() -> MessageQuery:
    start, end = month_bounds(2026, 7)
    return MessageQuery(start=start, end=end, folder_ids=("inbox",), subject_contains="receipt", limit=3)


def test_folder_listing_builds_tree() -> None:
    fake = FakeGraph()
    folders = GraphConnector("token", http=fake.client()).folders()
    assert [(f.id, f.display_path) for f in folders] == [("inbox", "Inbox"), ("receipts", "Inbox/Receipts"), ("archive", "Archive")]
    assert folders[0].well_known


def test_date_range_filter_and_client_header_filter() -> None:
    fake = FakeGraph()
    messages = GraphConnector("token", http=fake.client(), address="ada@example.edu").search(query())
    assert [m.id for m in messages] == ["m1"]
    req = next(r for r in fake.requests if r.url.path.endswith("/mailFolders/inbox/messages"))
    qs = parse_qs(req.url.query.decode())
    assert qs["$filter"] == ["receivedDateTime ge 2026-07-01T00:00:00Z and receivedDateTime lt 2026-08-01T00:00:00Z"]
    assert "$search" not in qs


def test_header_parsing_attachment_download_and_raw_mime() -> None:
    fake = FakeGraph()
    connector = GraphConnector("token", http=fake.client())
    header = connector.search(query())[0]
    assert header.from_address == "sales@example.com"
    assert [r.filename for r in header.attachment_refs] == ["receipt.pdf", "pixel.png"]
    assert header.attachment_refs[1].inline
    attachment = connector.fetch_attachment("m1", "a1")
    assert attachment.content == b"pdf-bytes"
    assert attachment.ref.filename == "receipt.pdf"
    body = connector.fetch_body("m1")
    assert body.mime and b"Subject: Receipt" in body.mime
    assert body.html == "<p>Receipt</p>"


def test_401_raises_reauth_required() -> None:
    fake = FakeGraph()
    fake.fail_auth = True
    with pytest.raises(ReauthRequired):
        GraphConnector("token", http=fake.client()).status()


def test_429_retries_then_succeeds() -> None:
    fake = FakeGraph()
    fake.throttle_once = True
    assert GraphConnector("token", http=fake.client()).status().ok
    assert len(fake.requests) == 2


def test_odata_single_quotes_are_escaped() -> None:
    assert _odata_string_literal("Bob's Books") == "'Bob''s Books'"
