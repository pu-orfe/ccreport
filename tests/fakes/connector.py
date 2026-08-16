"""An in-process mail connector, for testing everything above the providers.

The three real connectors have their own fakes at the HTTP and IMAP level. This
one is deliberately higher up: it satisfies :class:`MailConnector` directly so
that browse, selection, submission and bundling can be exercised without any
provider semantics in the way.
"""

from __future__ import annotations

import datetime as _dt
from email.message import EmailMessage

from ccreport.connectors.base import (
    Attachment,
    AttachmentRef,
    ConnectorStatus,
    MailFolder,
    MessageBody,
    MessageHeader,
    MessageQuery,
)
from ccreport.errors import ConnectorError


def message_bytes(subject: str, *, attachment: tuple[str, bytes, str, str] | None = None) -> bytes:
    """A small RFC 822 message, optionally carrying one attachment."""
    msg = EmailMessage()
    msg["From"] = "Vendor Billing <billing@vendor.example>"
    msg["To"] = "ada@princeton.edu"
    msg["Subject"] = subject
    msg["Date"] = "Sun, 05 Jul 2026 12:00:00 +0000"
    msg["Message-ID"] = "<fake@vendor.example>"
    msg.set_content(f"{subject}\nTotal: $42.50\n")
    if attachment:
        filename, content, maintype, subtype = attachment
        msg.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)
    return msg.as_bytes()


def header(
    message_id: str,
    subject: str = "Your receipt",
    *,
    day: int = 5,
    month: int = 7,
    year: int = 2026,
    sender: str = "billing@vendor.example",
    sender_name: str = "Vendor Billing",
    attachments: tuple[AttachmentRef, ...] = (),
    snippet: str = "Total: $42.50",
) -> MessageHeader:
    return MessageHeader(
        id=message_id,
        subject=subject,
        from_name=sender_name,
        from_address=sender,
        to=["ada@princeton.edu"],
        received_at=_dt.datetime(year, month, day, 12, 0, tzinfo=_dt.UTC),
        has_attachments=bool(attachments),
        attachment_refs=list(attachments),
        snippet=snippet,
    )


PDF_REF = AttachmentRef(
    id="att-pdf", filename="receipt.pdf", content_type="application/pdf", size_bytes=17
)
LOGO_REF = AttachmentRef(
    id="att-logo", filename="logo.png", content_type="image/png", size_bytes=9, inline=True,
    content_id="logo",
)


class FakeConnector:
    """A mailbox that answers from memory."""

    def __init__(
        self,
        headers: list[MessageHeader] | None = None,
        *,
        provider: str = "graph",
        address: str = "ada@princeton.edu",
        bodies: dict[str, bytes] | None = None,
        attachments: dict[tuple[str, str], bytes] | None = None,
        folders: list[MailFolder] | None = None,
        warnings: tuple[str, ...] = (),
    ):
        self.provider = provider
        self.address = address
        self.headers = headers or []
        self.bodies = bodies or {}
        self.attachments = attachments or {}
        self._folders = folders or [
            MailFolder(id="INBOX", name="Inbox", path="Inbox", well_known=True),
            MailFolder(id="ARCHIVE", name="Archive", path="Archive"),
        ]
        self.warnings = warnings
        self.searches: list[MessageQuery] = []
        self.fetched_bodies: list[str] = []

    # -- MailConnector
    def status(self) -> ConnectorStatus:
        return ConnectorStatus(
            ok=True,
            provider=self.provider,
            address=self.address,
            detail="fake connector reachable",
            warnings=self.warnings,
        )

    def folders(self) -> list[MailFolder]:
        return list(self._folders)

    def search(self, query: MessageQuery) -> list[MessageHeader]:
        self.searches.append(query)
        out = []
        for item in self.headers:
            if item.received_at and not (query.start <= item.received_at < query.end):
                continue
            if query.has_attachments_only and not item.attachment_refs:
                continue
            out.append(item)
        return out[: query.limit]

    def _header(self, message_id: str) -> MessageHeader:
        for item in self.headers:
            if item.id == message_id:
                return item
        raise ConnectorError(f"no such message {message_id!r}")

    def attachment_refs(self, message_id: str) -> list[AttachmentRef]:
        return list(self._header(message_id).attachment_refs)

    def fetch_body(self, message_id: str) -> MessageBody:
        self.fetched_bodies.append(message_id)
        raw = self.bodies.get(message_id)
        if raw is None:
            raw = message_bytes(self._header(message_id).subject)
        return MessageBody(id=message_id, mime=raw)

    def fetch_attachment(self, message_id: str, attachment_id: str) -> Attachment:
        ref = next(
            (r for r in self._header(message_id).attachment_refs if r.id == attachment_id), None
        )
        if ref is None:
            raise ConnectorError(f"no attachment {attachment_id!r} on {message_id!r}")
        content = self.attachments.get((message_id, attachment_id), b"%PDF-1.4 receipt\n")
        return Attachment(ref=ref, content=content)


__all__ = ["LOGO_REF", "PDF_REF", "FakeConnector", "header", "message_bytes"]
