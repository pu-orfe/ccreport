"""Render selected messages and receipt attachments into stable artifacts."""

from __future__ import annotations

import base64
import contextlib
import datetime as _dt
import hashlib
import html
import io
import re
import sys
import zlib
from dataclasses import dataclass, field
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path

from .connectors.base import Attachment, MessageBody, MessageHeader
from .errors import RenderError
from .settings import Settings, get_settings

_REMOTE_IMG_RE = re.compile(r"<img\b([^>]*?)\bsrc=[\"']https?://[^\"']+[\"']([^>]*)>", re.IGNORECASE)
_CID_RE = re.compile(r"cid:([^\"') >]+)", re.IGNORECASE)
_PAGE_RE = re.compile(rb"/Type\s*/Page\b(?!s)")
_STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)

_ALLOWED_TAGS = {
    "a",
    "abbr",
    "b",
    "blockquote",
    "body",
    "br",
    "caption",
    "code",
    "col",
    "colgroup",
    "dd",
    "div",
    "dl",
    "dt",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "head",
    "hr",
    "html",
    "i",
    "img",
    "li",
    "meta",
    "ol",
    "p",
    "pre",
    "s",
    "small",
    "span",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "u",
    "ul",
}
_ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "img": {"src", "alt", "title", "width", "height"},
    "td": {"colspan", "rowspan", "align"},
    "th": {"colspan", "rowspan", "align"},
    "table": {"summary"},
    "meta": {"charset"},
    "*": {"class"},
}


@dataclass(slots=True)
class RenderedDocument:
    content: bytes
    renderer: str
    page_count: int | None
    sha256: str
    warnings: list[str] = field(default_factory=list)


def available_renderers() -> list[str]:
    renderers: list[str] = []
    if _can_import_weasyprint():
        renderers.append("weasyprint")
    if _can_import_playwright():
        renderers.append("playwright")
    return renderers


def _can_import_weasyprint() -> bool:
    # WeasyPrint writes its "could not import some external libraries" notice to
    # stdout on import. stdout is data here, so the notice is sent to stderr with
    # the rest of the diagnostics rather than corrupting a JSON document.
    with contextlib.redirect_stdout(sys.stderr):
        try:
            import weasyprint  # noqa: F401
        except Exception:
            return False
    return True


def _can_import_playwright() -> bool:
    with contextlib.redirect_stdout(sys.stderr):
        try:
            import playwright.sync_api  # noqa: F401
        except Exception:
            return False
    return True


def normalize_attachment(attachment: Attachment, settings: Settings | None = None) -> tuple[bytes, str, str]:
    settings = settings or get_settings()
    content = attachment.content
    ref = attachment.ref
    limit = settings.max_attachment_mb * 1024 * 1024
    declared_size = ref.size_bytes if ref.size_bytes is not None else len(content)
    if declared_size > limit or len(content) > limit:
        raise ValueError(f"attachment exceeds {settings.max_attachment_mb} MB limit")

    filename = ref.filename or "attachment"
    ctype = (ref.content_type or "").lower()
    suffix = Path(filename).suffix.lower()
    if ctype == "application/pdf" or suffix == ".pdf":
        return content, filename, "application/pdf"
    if ctype in {"image/jpeg", "image/png"} or suffix in {".jpg", ".jpeg", ".png"}:
        if ctype in {"image/jpeg", "image/png"}:
            return content, filename, ctype
        return content, filename, "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/png"
    if ctype in {"image/heic", "image/heif"} or suffix in {".heic", ".heif"}:
        return _convert_image(content, filename, "JPEG", ".jpg", register_heif=True)
    if ctype == "image/tiff" or suffix in {".tif", ".tiff"}:
        return _convert_image(content, filename, "PDF", ".pdf", register_heif=False)
    raise ValueError(f"unsupported attachment type: {ref.content_type or suffix or 'unknown'}")


def pdf_page_count(data: bytes) -> int | None:
    """Count pages in a PDF without a parser dependency.

    Modern producers, WeasyPrint among them, pack page objects into compressed
    object streams, so scanning the raw bytes for ``/Type /Page`` finds nothing
    and quietly reports an unknown page count. Compressed streams are therefore
    inflated and scanned too — a page count that is always ``None`` is worse
    than no column at all, because it looks like data.
    """
    try:
        count = len(_PAGE_RE.findall(data))
        if count:
            return count
        for compressed in _STREAM_RE.findall(data):
            try:
                inflated = zlib.decompress(compressed)
            except zlib.error:
                continue
            count += len(_PAGE_RE.findall(inflated))
    except Exception:
        return None
    return count or None


def render_message_to_pdf(
    body: MessageBody,
    header: MessageHeader,
    *,
    settings: Settings | None = None,
    account_address: str | None = None,
) -> RenderedDocument:
    settings = settings or get_settings()
    warnings: list[str] = []
    html_doc = _assemble_html(body, header, settings=settings, account_address=account_address, warnings=warnings)
    causes: list[BaseException] = []

    try:
        import weasyprint

        # Rendering in two steps so the page count comes from the layout itself
        # rather than from guessing at the bytes it produced.
        document = weasyprint.HTML(string=html_doc, base_url=".").render()
        content = document.write_pdf()
        digest = hashlib.sha256(content).hexdigest()
        pages = len(document.pages) or pdf_page_count(content)
        return RenderedDocument(content, "weasyprint", pages, digest, warnings)
    except Exception as exc:
        causes.append(exc)

    if settings.enable_playwright_fallback:
        try:
            content = _render_with_playwright(html_doc)
            digest = hashlib.sha256(content).hexdigest()
            return RenderedDocument(content, "playwright", pdf_page_count(content), digest, warnings)
        except Exception as exc:
            causes.append(exc)

    error = RenderError("no renderer could produce a PDF")
    if causes:
        error.__cause__ = causes[-1]
        error.causes = causes  # type: ignore[attr-defined]
    raise error


def _assemble_html(
    body: MessageBody,
    header: MessageHeader,
    *,
    settings: Settings,
    account_address: str | None,
    warnings: list[str] | None = None,
) -> str:
    warnings = warnings if warnings is not None else []
    source, inline_parts, mime_headers, source_bytes = _parse_body(body)
    source = _rewrite_cid_images(source, inline_parts, warnings)
    source = _handle_remote_images(source, settings.allow_remote_images, warnings)
    source = _sanitize_html(source)
    provenance = _provenance_header(header, mime_headers, source_bytes, account_address)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
body {{ font-family: sans-serif; font-size: 12px; line-height: 1.35; }}
.ccreport-provenance {{ border: 1px solid #999; padding: 8px; margin-bottom: 12px; }}
.ccreport-provenance div {{ margin: 2px 0; }}
.ccreport-remote-image-placeholder {{ border: 1px dashed #888; color: #555; padding: 8px; display: inline-block; }}
table {{ border-collapse: collapse; }} td, th {{ padding: 2px 4px; }}
</style></head><body>{provenance}<main>{source}</main></body></html>"""


def _parse_body(body: MessageBody) -> tuple[str, dict[str, tuple[bytes, str]], Message | None, bytes]:
    if body.mime:
        message = BytesParser(policy=policy.default).parsebytes(body.mime)
        html_part = None
        text_part = None
        inline_parts: dict[str, tuple[bytes, str]] = {}
        for part in message.walk():
            if part.is_multipart():
                continue
            ctype = part.get_content_type()
            disposition = (part.get_content_disposition() or "").lower()
            cid = (part.get("Content-ID") or "").strip("<>")
            if cid and (disposition == "inline" or ctype.startswith("image/")):
                inline_parts[cid] = (part.get_payload(decode=True) or b"", ctype)
            if ctype == "text/html" and html_part is None:
                html_part = part.get_content()
            elif ctype == "text/plain" and text_part is None:
                text_part = part.get_content()
        if html_part is not None:
            return html_part, inline_parts, message, body.mime
        if text_part is not None:
            return _text_to_html(text_part), inline_parts, message, body.mime
    if body.html:
        return body.html, {}, None, body.html.encode("utf-8")
    if body.text:
        return _text_to_html(body.text), {}, None, body.text.encode("utf-8")
    return "", {}, None, b""


def _sanitize_html(source: str) -> str:
    import nh3

    return nh3.clean(
        source,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes={"http", "https", "mailto", "data"},
        strip_comments=True,
    )


def _rewrite_cid_images(
    source: str, inline_parts: dict[str, tuple[bytes, str]], warnings: list[str]) -> str:
    def replace(match: re.Match[str]) -> str:
        cid = html.unescape(match.group(1)).strip("<>")
        part = inline_parts.get(cid)
        if not part:
            warnings.append(f"missing inline image: {cid}")
            return ""
        content, ctype = part
        encoded = base64.b64encode(content).decode("ascii")
        return f"data:{ctype};base64,{encoded}"

    return _CID_RE.sub(replace, source)


def _handle_remote_images(source: str, allow_remote: bool, warnings: list[str]) -> str:
    if allow_remote:
        return source

    def replace(_match: re.Match[str]) -> str:
        warnings.append("blocked remote image")
        return '<div class="ccreport-remote-image-placeholder">Remote image blocked</div>'

    return _REMOTE_IMG_RE.sub(replace, source)


def _provenance_header(
    header: MessageHeader,
    mime_headers: Message | None,
    source_bytes: bytes,
    account_address: str | None,
) -> str:
    def pick(attr: str, mime_name: str) -> str:
        value = getattr(header, attr, None)
        if value:
            if isinstance(value, list):
                return ", ".join(value)
            if isinstance(value, _dt.datetime):
                return value.isoformat()
            return str(value)
        if mime_headers and mime_headers.get(mime_name):
            raw = str(mime_headers.get(mime_name))
            if mime_name.lower() == "date":
                try:
                    return parsedate_to_datetime(raw).isoformat()
                except Exception:
                    return raw
            return raw
        return ""

    rows = [
        ("From", pick("from_address", "From")),
        ("To", pick("to", "To")),
        ("Date", pick("received_at", "Date")),
        ("Subject", pick("subject", "Subject")),
        ("Message-ID", mime_headers.get("Message-ID", "") if mime_headers else ""),
        ("Captured", _dt.datetime.now(_dt.UTC).isoformat()),
        ("Source account", account_address or header.account_id or ""),
        ("Source MIME SHA-256", hashlib.sha256(source_bytes).hexdigest()),
    ]
    rendered = "".join(
        f"<div><strong>{html.escape(label)}:</strong> {html.escape(str(value))}</div>"
        for label, value in rows
    )
    return f'<section class="ccreport-provenance">{rendered}</section>'


def _text_to_html(text: str) -> str:
    return f"<pre>{html.escape(text)}</pre>"


def _render_with_playwright(html_doc: str) -> bytes:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html_doc, wait_until="load")
            return page.pdf(format="Letter", print_background=True)
        finally:
            browser.close()


def _convert_image(
    content: bytes,
    filename: str,
    image_format: str,
    suffix: str,
    *,
    register_heif: bool,
) -> tuple[bytes, str, str]:
    if register_heif:
        from pillow_heif import register_heif_opener

        register_heif_opener()
    from PIL import Image

    with Image.open(io.BytesIO(content)) as image:
        if image_format == "JPEG" and image.mode not in {"RGB", "L"}:
            image = image.convert("RGB")
        output = io.BytesIO()
        image.save(output, format=image_format)
    new_name = str(Path(filename).with_suffix(suffix))
    content_type = "application/pdf" if image_format == "PDF" else "image/jpeg"
    return output.getvalue(), new_name, content_type


__all__ = [
    "RenderedDocument",
    "available_renderers",
    "normalize_attachment",
    "pdf_page_count",
    "render_message_to_pdf",
]
