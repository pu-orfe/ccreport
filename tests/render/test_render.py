from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from ccreport.connectors.base import Attachment, AttachmentRef, MessageBody, MessageHeader
from ccreport.errors import RenderError
from ccreport.render import (
    _assemble_html,
    available_renderers,
    normalize_attachment,
    pdf_page_count,
    render_message_to_pdf,
)
from ccreport.settings import Settings

DATA = Path("tests/data")


def _body(name: str) -> MessageBody:
    return MessageBody(id=name, mime=(DATA / name).read_bytes())


def _header() -> MessageHeader:
    return MessageHeader(
        id="m1",
        account_id="acct1",
        subject="Fallback Subject",
        from_address="sender@example.com",
        to=["faculty@princeton.edu"],
    )


def _html(name: str, *, allow_remote_images: bool = False) -> str:
    return _assemble_html(
        _body(name),
        _header(),
        settings=Settings(allow_remote_images=allow_remote_images),
        account_address="faculty@princeton.edu",
    )


def test_hostile_html_is_sanitized_without_pdf_engine() -> None:
    html = _html("hostile.eml")
    assert "<script" not in html
    assert "onerror" not in html
    assert "onclick" not in html
    assert "Safe total" in html


def test_cid_images_become_data_uris() -> None:
    html = _html("cid_image.eml")
    assert "cid:logo1" not in html
    assert "data:image/png;base64,iVBORw0KGgo=" in html


def test_remote_images_blocked_by_default_and_allowed_when_flipped() -> None:
    body = MessageBody(id="r", html='<p>Receipt</p><img src="https://example.com/pixel.png">')
    blocked = _assemble_html(body, _header(), settings=Settings(), account_address=None)
    allowed = _assemble_html(
        body,
        _header(),
        settings=Settings(allow_remote_images=True),
        account_address=None,
    )
    assert "Remote image blocked" in blocked
    assert "https://example.com/pixel.png" not in blocked
    assert "https://example.com/pixel.png" in allowed


def test_text_only_becomes_preformatted_html() -> None:
    html = _html("text_only.eml")
    assert "<pre>Plain receipt\nTotal $9.99\n</pre>" in html


def test_provenance_header_contains_source_hash() -> None:
    body = _body("html_receipt.eml")
    html = _assemble_html(
        body,
        _header(),
        settings=Settings(),
        account_address="faculty@princeton.edu",
    )
    source_hash = hashlib.sha256(body.mime or b"").hexdigest()
    assert "Source MIME SHA-256" in html
    assert source_hash in html
    assert "faculty@princeton.edu" in html
    assert "Message-ID" in html


def test_render_error_when_no_renderer_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "weasyprint", None)
    settings = Settings(enable_playwright_fallback=False)
    with pytest.raises(RenderError) as excinfo:
        render_message_to_pdf(MessageBody(id="x", html="<p>Receipt</p>"), _header(), settings=settings)
    assert "causes" in excinfo.value.__dict__


def test_available_renderers_returns_list() -> None:
    assert isinstance(available_renderers(), list)


def test_pdf_page_count_is_tolerant() -> None:
    assert pdf_page_count(b"%PDF /Type /Page /Type /Pages /Type /Page\n") == 2
    assert pdf_page_count(b"not a pdf") is None


def test_normalize_attachment_passthrough_and_limit() -> None:
    pdf = Attachment(AttachmentRef("a", "receipt.pdf", "application/pdf", 4), b"%PDF")
    assert normalize_attachment(pdf) == (b"%PDF", "receipt.pdf", "application/pdf")
    jpg = Attachment(AttachmentRef("a", "photo.jpg", "image/jpeg", 3), b"jpg")
    assert normalize_attachment(jpg) == (b"jpg", "photo.jpg", "image/jpeg")
    big = Attachment(AttachmentRef("a", "big.pdf", "application/pdf", 2 * 1024 * 1024), b"x")
    with pytest.raises(ValueError, match="exceeds"):
        normalize_attachment(big, Settings(max_attachment_mb=1))


def test_render_message_to_pdf_with_weasyprint() -> None:
    try:
        pytest.importorskip("weasyprint")
    except OSError as exc:
        pytest.skip(f"weasyprint import failed: {exc}")
    document = render_message_to_pdf(
        _body("html_receipt.eml"),
        _header(),
        settings=Settings(enable_playwright_fallback=False),
        account_address="faculty@princeton.edu",
    )
    assert document.renderer == "weasyprint"
    assert document.page_count and document.page_count >= 1
    assert document.sha256 == hashlib.sha256(document.content).hexdigest()
    assert b"%PDF" in document.content[:20]
