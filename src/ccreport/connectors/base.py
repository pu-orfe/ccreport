"""The mail connector contract.

Read-only is structural here, not a convention someone remembers to follow:
:class:`MailConnector` declares no method that mutates a mailbox. There is no
``delete``, no ``mark_read``, no ``move``. A connector cannot write because the
interface it implements offers nowhere to write to, and a reviewer can confirm
that by reading one short file instead of auditing three provider clients.

The types below are provider-neutral on purpose. Graph, Gmail and IMAP disagree
about nearly everything — folder identity, search syntax, how a body is encoded,
whether an attachment is inline — and every one of those disagreements is
resolved inside the connector so the heuristics, the renderer and the web layer
see one shape.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

#: Providers ccreport can talk to. Kept as plain strings so they can be stored
#: in a column and compared without importing this module.
PROVIDERS = ("graph", "gmail", "imap")

_ADDRESS_RE = re.compile(r"<([^>]+)>")


def parse_address(raw: str | None) -> tuple[str | None, str | None]:
    """Split ``"Ada Lovelace <ada@example.edu>"`` into name and address.

    Providers hand back sender information in at least four shapes. Normalising
    once, here, keeps ``(name, address)`` assumptions out of everything upstream.
    """
    if not raw:
        return None, None
    raw = raw.strip()
    match = _ADDRESS_RE.search(raw)
    if match:
        name = raw[: match.start()].strip().strip('"').strip()
        return (name or None), match.group(1).strip().lower()
    if "@" in raw:
        return None, raw.strip().strip("<>").lower()
    return raw or None, None


@dataclass(frozen=True, slots=True)
class MailFolder:
    """One selectable container of messages.

    Gmail labels, Graph mail folders and IMAP mailboxes all land here. ``id`` is
    whatever the provider needs to address it again; it is never parsed.
    """

    id: str
    name: str
    path: str | None = None
    total_count: int | None = None
    unread_count: int | None = None
    #: True for Inbox, Archive, Sent and similar — the ones worth pre-selecting.
    well_known: bool = False

    @property
    def display_path(self) -> str:
        return self.path or self.name


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    """An attachment we know exists but have not downloaded."""

    id: str
    filename: str
    content_type: str | None
    size_bytes: int | None
    inline: bool = False
    content_id: str | None = None

    @property
    def is_probably_receipt_media(self) -> bool:
        """PDFs and photographs; not signatures, calendar invites or tracking pixels."""
        if self.inline:
            return False
        ctype = (self.content_type or "").lower()
        name = (self.filename or "").lower()
        if ctype.startswith("image/") or ctype == "application/pdf":
            return True
        return name.endswith((".pdf", ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff"))


@dataclass(frozen=True, slots=True)
class Attachment:
    """A downloaded attachment."""

    ref: AttachmentRef
    content: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(slots=True)
class MessageHeader:
    """Enough of a message to list, search, score and select it.

    Deliberately does not carry the body. The browse view is built from these,
    and the body is fetched only for a message the user actually selected —
    which is what makes "we do not store your mail" a true statement rather than
    an aspiration.
    """

    id: str
    account_id: str | None = None
    folder_id: str | None = None
    subject: str = ""
    from_name: str | None = None
    from_address: str | None = None
    to: list[str] = field(default_factory=list)
    received_at: _dt.datetime | None = None
    has_attachments: bool = False
    attachment_refs: list[AttachmentRef] = field(default_factory=list)
    #: A short provider-supplied preview, when one is available cheaply.
    snippet: str = ""
    web_link: str | None = None

    # Filled in by ccreport.filters, not by connectors.
    receipt_score: int = 0
    likely_receipt: bool = False
    amount_hint_cents: int | None = None
    currency_hint: str | None = None
    vendor_hint: str | None = None
    matched_signals: list[str] = field(default_factory=list)

    @property
    def sender_display(self) -> str:
        return self.from_name or self.from_address or "(unknown sender)"

    @property
    def receipt_media_refs(self) -> list[AttachmentRef]:
        return [r for r in self.attachment_refs if r.is_probably_receipt_media]


@dataclass(slots=True)
class MessageBody:
    """The parts of a message needed to render it to PDF."""

    id: str
    html: str | None = None
    text: str | None = None
    #: Raw RFC 822 when the provider gives it to us. Preferred for rendering,
    #: because it carries inline images and the original headers together.
    mime: bytes | None = None

    @property
    def best_effort_html(self) -> str | None:
        return self.html or None


@dataclass(frozen=True, slots=True)
class MessageQuery:
    """A month of one mailbox, optionally narrowed by header search.

    ``start`` is inclusive and ``end`` exclusive, both timezone-aware UTC. The
    month arithmetic lives in ``ccreport.period`` so every connector agrees on
    where a month begins.
    """

    start: _dt.datetime
    end: _dt.datetime
    folder_ids: tuple[str, ...] = ()
    subject_contains: str | None = None
    from_contains: str | None = None
    has_attachments_only: bool = False
    limit: int = 500

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("MessageQuery bounds must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("MessageQuery end must be after start")


@dataclass(frozen=True, slots=True)
class ConnectorStatus:
    """What ``ccreport account test`` reports for one connection."""

    ok: bool
    provider: str
    address: str | None = None
    detail: str = ""
    needs_reauth: bool = False
    #: Provider-specific warnings worth showing without failing the check —
    #: e.g. a Google client whose consent posture expires tokens weekly.
    warnings: tuple[str, ...] = ()


@runtime_checkable
class MailConnector(Protocol):
    """Read-only access to one mailbox.

    Every method is a read. Adding a write here should feel like what it is: a
    change to the security posture of the whole application, visible in a diff
    to this file.
    """

    provider: str

    def status(self) -> ConnectorStatus:
        """Verify the credential and report what it can see."""
        ...

    def folders(self) -> list[MailFolder]:
        """List selectable folders, labels or mailboxes."""
        ...

    def search(self, query: MessageQuery) -> list[MessageHeader]:
        """Return headers matching ``query``. Never returns bodies."""
        ...

    def fetch_body(self, message_id: str) -> MessageBody:
        """Fetch the body of one message, for rendering."""
        ...

    def fetch_attachment(self, message_id: str, attachment_id: str) -> Attachment:
        """Download one attachment."""
        ...


__all__ = [
    "PROVIDERS",
    "Attachment",
    "AttachmentRef",
    "ConnectorStatus",
    "MailConnector",
    "MailFolder",
    "MessageBody",
    "MessageHeader",
    "MessageQuery",
    "parse_address",
]
