from __future__ import annotations

import base64
import datetime as _dt
import email
import imaplib
import re
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime

from ccreport.errors import ConnectorError, ReauthRequired

from .base import (
    Attachment,
    AttachmentRef,
    ConnectorStatus,
    MailFolder,
    MessageBody,
    MessageHeader,
    MessageQuery,
    parse_address,
)

_MUTATING = {"STORE", "APPEND", "EXPUNGE", "COPY", "MOVE", "SELECT"}
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def decode_modified_utf7(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        if value[i] != "&":
            out.append(value[i])
            i += 1
            continue
        j = value.find("-", i)
        if j == -1:
            out.append(value[i:])
            break
        chunk = value[i + 1 : j]
        if not chunk:
            out.append("&")
        else:
            data = chunk.replace(",", "/")
            data += "=" * (-len(data) % 4)
            out.append(base64.b64decode(data).decode("utf-16-be"))
        i = j + 1
    return "".join(out)


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def _imap_date(value: _dt.datetime) -> str:
    v = value.astimezone(_dt.UTC)
    return f"{v.day:02d}-{_MONTHS[v.month - 1]}-{v.year:04d}"


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', r'\"') + '"'


def _ok(status: str, data) -> list[bytes]:
    if status != "OK":
        text = b" ".join(x for x in (data or []) if isinstance(x, bytes)).decode("utf-8", "replace")
        if "AUTHENTICATIONFAILED" in text.upper():
            raise ReauthRequired("IMAP credential was rejected; reconnect this account.")
        raise ConnectorError(f"IMAP returned {status}: {text}")
    return [x for x in (data or []) if isinstance(x, bytes)]


class ImapConnector:
    provider = "imap"

    def __init__(self, host: str, port: int, username: str, app_password: str, *, ssl_context=None, imap_factory=None):
        self._host = host
        self._port = port
        self._username = username
        self._password = app_password
        self._ssl_context = ssl_context
        self._factory = imap_factory or imaplib.IMAP4_SSL
        self._imap = None
        self._folder = "INBOX"
        self._uidvalidity = "0"

    def _conn(self):
        if self._imap is None:
            if self._ssl_context is None:
                self._imap = self._factory(self._host, self._port)
            else:
                self._imap = self._factory(self._host, self._port, ssl_context=self._ssl_context)
            status, data = self._imap.login(self._username, self._password)
            _ok(status, data)
        return self._imap

    def _examine(self, folder: str = "INBOX") -> None:
        imap = self._conn()
        if hasattr(imap, "examine"):
            status, data = imap.examine(folder)
        else:
            status, data = imap.select(folder, readonly=True)
        lines = _ok(status, data)
        self._folder = folder
        uidvalidity = None
        if hasattr(imap, "response"):
            try:
                _, values = imap.response("UIDVALIDITY")
                if values and values[0]:
                    uidvalidity = values[0].decode() if isinstance(values[0], bytes) else str(values[0])
            except Exception:
                uidvalidity = None
        for line in lines:
            m = re.search(rb"UIDVALIDITY\s+(\d+)", line)
            if m:
                uidvalidity = m.group(1).decode()
        self._uidvalidity = uidvalidity or self._uidvalidity or "0"

    def status(self) -> ConnectorStatus:
        self._examine("INBOX")
        return ConnectorStatus(ok=True, provider=self.provider, address=self._username, detail="IMAP reachable")

    def folders(self) -> list[MailFolder]:
        status, data = self._conn().list()
        lines = _ok(status, data)
        return [self._parse_list(line) for line in lines if line]

    def _parse_list(self, line: bytes) -> MailFolder:
        text = line.decode("utf-8", "replace")
        m = re.match(r"\((?P<flags>[^)]*)\)\s+(?P<delim>NIL|\".*?\")\s+(?P<name>.*)", text)
        if not m:
            name = decode_modified_utf7(text.strip().strip('"'))
            return MailFolder(id=name, name=name, path=name)
        delim = None if m.group("delim") == "NIL" else m.group("delim").strip('"')
        raw = m.group("name").strip()
        name = raw[1:-1].replace(r'\"', '"') if raw.startswith('"') and raw.endswith('"') else raw
        decoded = decode_modified_utf7(name)
        display = decoded.split(delim)[-1] if delim else decoded
        flags = m.group("flags").lower()
        return MailFolder(id=decoded, name=display, path=decoded, well_known="\\inbox" in flags or decoded.upper() in {"INBOX", "SENT", "TRASH", "DRAFTS", "ARCHIVE"})

    def search(self, query: MessageQuery) -> list[MessageHeader]:
        folder = query.folder_ids[0] if query.folder_ids else "INBOX"
        self._examine(folder)
        criteria = ["SINCE", _imap_date(query.start), "BEFORE", _imap_date(query.end)]
        if query.subject_contains:
            criteria.extend(["HEADER", "SUBJECT", _quote(query.subject_contains)])
        if query.from_contains:
            criteria.extend(["FROM", _quote(query.from_contains)])
        status, data = self._conn().uid("SEARCH", None, *criteria)
        ids = b" ".join(_ok(status, data)).decode().split()
        out: list[MessageHeader] = []
        for uid in ids[: query.limit]:
            out.append(self._fetch_header(uid, folder))
        return out

    def _fetch_header(self, uid: str, folder: str) -> MessageHeader:
        command = "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE)] RFC822.SIZE BODYSTRUCTURE)"
        status, data = self._conn().uid("FETCH", uid, command)
        lines = _ok(status, data)
        blob = b"\r\n".join(lines)
        if b"\r\n\r\n" in blob:
            header_bytes = blob.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
        else:
            header_bytes = blob
        msg = email.message_from_bytes(header_bytes)
        name, addr = parse_address(_decode_header(msg.get("From")))
        received = None
        if msg.get("Date"):
            received = parsedate_to_datetime(msg.get("Date")).astimezone(_dt.UTC)
        bodystructure = blob.decode("utf-8", "replace").lower()
        refs = self._attachment_refs(uid) if "attachment" in bodystructure else []
        return MessageHeader(id=f"{self._uidvalidity}:{uid}", account_id=self._username, folder_id=folder, subject=_decode_header(msg.get("Subject")), from_name=name, from_address=addr, to=[a.lower() for _, a in getaddresses([msg.get("To", "")]) if a], received_at=received, has_attachments=bool(refs), attachment_refs=refs)

    def _split_id(self, message_id: str) -> str:
        uidvalidity, uid = message_id.split(":", 1)
        if uidvalidity != self._uidvalidity:
            self._examine(self._folder)
        if uidvalidity != self._uidvalidity:
            raise ConnectorError("IMAP UIDVALIDITY changed; reconnect and search again before fetching this message.")
        return uid

    def _fetch_mime(self, uid: str) -> bytes:
        status, data = self._conn().uid("FETCH", uid, "(BODY.PEEK[])")
        lines = _ok(status, data)
        for item in lines:
            if b"Subject:" in item or b"Content-Type:" in item or b"From:" in item:
                return item
        return b"\r\n".join(lines)

    def _attachment_refs(self, uid: str) -> list[AttachmentRef]:
        msg = email.message_from_bytes(self._fetch_mime(uid))
        refs: list[AttachmentRef] = []
        for index, part in enumerate(msg.walk()):
            if part.is_multipart():
                continue
            filename = part.get_filename()
            if not filename:
                continue
            disposition = (part.get_content_disposition() or "").lower()
            refs.append(AttachmentRef(id=f"part-{index}", filename=_decode_header(filename), content_type=part.get_content_type(), size_bytes=len(part.get_payload(decode=True) or b""), inline=disposition == "inline", content_id=(part.get("Content-ID") or "").strip("<>") or None))
        return refs

    def attachment_refs(self, message_id: str) -> list[AttachmentRef]:
        return self._attachment_refs(self._split_id(message_id))

    def fetch_body(self, message_id: str) -> MessageBody:
        uid = self._split_id(message_id)
        return MessageBody(id=message_id, mime=self._fetch_mime(uid))

    def fetch_attachment(self, message_id: str, attachment_id: str) -> Attachment:
        uid = self._split_id(message_id)
        msg = email.message_from_bytes(self._fetch_mime(uid))
        for index, part in enumerate(msg.walk()):
            if part.is_multipart() or not part.get_filename():
                continue
            ref = AttachmentRef(id=f"part-{index}", filename=_decode_header(part.get_filename()), content_type=part.get_content_type(), size_bytes=len(part.get_payload(decode=True) or b""), inline=(part.get_content_disposition() or "").lower() == "inline", content_id=(part.get("Content-ID") or "").strip("<>") or None)
            if ref.id == attachment_id:
                return Attachment(ref=ref, content=part.get_payload(decode=True) or b"")
        raise ConnectorError(f"IMAP attachment {attachment_id!r} was not found")


__all__ = ["ImapConnector", "decode_modified_utf7"]
