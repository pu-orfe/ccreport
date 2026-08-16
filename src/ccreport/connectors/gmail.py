from __future__ import annotations

import base64
import datetime as _dt
import secrets
import time
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx

from ccreport.errors import ConnectorError, ReauthRequired
from ccreport.httpclient import shared_client

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

_GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
_WELL_KNOWN = {"INBOX", "SENT", "STARRED", "IMPORTANT", "TRASH", "DRAFT", "SPAM", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS", "CATEGORY_UPDATES", "CATEGORY_FORUMS"}


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _date_token(value: _dt.datetime) -> str:
    return value.astimezone(_dt.UTC).strftime("%Y/%m/%d")


class GmailConnector:
    provider = "gmail"

    def __init__(self, access_token: str, *, http: httpx.Client | None = None, address: str | None = None, posture: str | None = None):
        self._token = access_token
        self._http = http or shared_client()
        self._address = address
        self._posture = (posture or "").strip().lower() or None

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        url = path if path.startswith("http") else f"{_GMAIL}{path}"
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self._token}"
        last: httpx.Response | None = None
        for attempt in range(4):
            resp = self._http.request(method, url, headers=headers, **kwargs)
            if resp.status_code == 401:
                raise ReauthRequired("Gmail credential is no longer valid; reconnect this account.")
            if resp.status_code == 400 and "invalid_grant" in resp.text:
                raise ReauthRequired("Gmail credential was revoked or expired; reconnect this account.")
            if resp.status_code in {403, 429} and self._rate_limited(resp):
                last = resp
                if attempt < 3:
                    time.sleep(min(0.1 * (2**attempt) + (secrets.randbelow(10) / 1000), 1.0))
                    continue
            if resp.status_code >= 400:
                raise ConnectorError(f"Gmail returned HTTP {resp.status_code}: {resp.text}")
            return resp
        raise ConnectorError(f"Gmail rate-limited the request after retries: HTTP {last.status_code if last else 'unknown'}")

    def _rate_limited(self, resp: httpx.Response) -> bool:
        try:
            text = resp.text
        except Exception:
            return True
        return resp.status_code == 429 or "rateLimitExceeded" in text or "userRateLimitExceeded" in text

    def status(self) -> ConnectorStatus:
        data = self._request("GET", "/profile").json()
        warnings: tuple[str, ...] = ()
        if self._posture in {"testing", "unknown"}:
            warnings = ("Google OAuth client posture may expire refresh tokens after 7 days; reconnect may be required.",)
        return ConnectorStatus(ok=True, provider=self.provider, address=self._address or data.get("emailAddress"), detail="Gmail reachable", warnings=warnings)

    def folders(self) -> list[MailFolder]:
        labels = self._request("GET", "/labels").json().get("labels", [])
        out: list[MailFolder] = []
        for label in labels:
            detail = self._request("GET", f"/labels/{quote(label['id'])}").json()
            lid = detail.get("id", label["id"])
            name = detail.get("name", label.get("name", lid))
            out.append(MailFolder(id=lid, name=name, path=name, total_count=detail.get("messagesTotal"), unread_count=detail.get("messagesUnread"), well_known=lid in _WELL_KNOWN or detail.get("type") == "system"))
        return out

    def search(self, query: MessageQuery) -> list[MessageHeader]:
        q = self._query(query)
        out: list[MessageHeader] = []
        token: str | None = None
        while len(out) < query.limit:
            params = {"q": q, "maxResults": str(min(100, query.limit - len(out)))}
            if token:
                params["pageToken"] = token
            data = self._request("GET", "/messages", params=params).json()
            ids = [m["id"] for m in data.get("messages", [])]
            for mid in ids:
                out.append(self._metadata(mid, query.folder_ids[0] if query.folder_ids else None))
                if len(out) >= query.limit:
                    break
            token = data.get("nextPageToken")
            if not token:
                break
        return out

    def _query(self, query: MessageQuery) -> str:
        parts = [f"after:{_date_token(query.start)}", f"before:{_date_token(query.end)}"]
        if query.has_attachments_only:
            parts.append("has:attachment")
        if query.subject_contains:
            escaped = query.subject_contains.replace('"', r'\"')
            parts.append(f'subject:"{escaped}"')
        if query.from_contains:
            parts.append(f"from:{query.from_contains}")
        for folder in query.folder_ids:
            parts.append(f"label:{folder}")
        return " ".join(parts)

    def _metadata(self, message_id: str, folder_id: str | None) -> MessageHeader:
        params = [("format", "metadata")]
        for h in ("Subject", "From", "To", "Date"):
            params.append(("metadataHeaders", h))
        data = self._request("GET", f"/messages/{quote(message_id)}", params=params).json()
        headers = {h.get("name", "").lower(): h.get("value", "") for h in (data.get("payload") or {}).get("headers", [])}
        name, addr = parse_address(headers.get("from"))
        received = None
        if headers.get("date"):
            received = parsedate_to_datetime(headers["date"]).astimezone(_dt.UTC)
        elif data.get("internalDate"):
            received = _dt.datetime.fromtimestamp(int(data["internalDate"]) / 1000, _dt.UTC)
        refs = self._attachment_refs_from_payload(data.get("payload") or {})
        return MessageHeader(id=message_id, account_id=self._address, folder_id=folder_id, subject=headers.get("subject", ""), from_name=name, from_address=addr, to=[a.strip().lower() for a in headers.get("to", "").split(",") if a.strip()], received_at=received, has_attachments=bool(refs), attachment_refs=refs, snippet=data.get("snippet") or "")

    def _attachment_refs_from_payload(self, payload: dict) -> list[AttachmentRef]:
        refs: list[AttachmentRef] = []

        def walk(part: dict) -> None:
            filename = part.get("filename") or ""
            body = part.get("body") or {}
            aid = body.get("attachmentId")
            if filename and aid:
                headers = {h.get("name", "").lower(): h.get("value", "") for h in part.get("headers", [])}
                disposition = headers.get("content-disposition", "").lower()
                cid = headers.get("content-id", "").strip("<>") or None
                refs.append(AttachmentRef(id=aid, filename=filename, content_type=part.get("mimeType"), size_bytes=body.get("size"), inline="inline" in disposition, content_id=cid))
            for child in part.get("parts", []) or []:
                walk(child)

        walk(payload)
        return refs

    def attachment_refs(self, message_id: str) -> list[AttachmentRef]:
        return self._metadata(message_id, None).attachment_refs

    def fetch_body(self, message_id: str) -> MessageBody:
        data = self._request("GET", f"/messages/{quote(message_id)}", params={"format": "raw"}).json()
        return MessageBody(id=message_id, mime=_b64url_decode(data.get("raw", "")))

    def fetch_attachment(self, message_id: str, attachment_id: str) -> Attachment:
        meta = self._metadata(message_id, None)
        refs = {r.id: r for r in meta.attachment_refs}
        ref = refs.get(attachment_id) or AttachmentRef(attachment_id, attachment_id, None, None)
        data = self._request("GET", f"/messages/{quote(message_id)}/attachments/{quote(attachment_id)}").json()
        return Attachment(ref=ref, content=_b64url_decode(data.get("data", "")))


__all__ = ["GmailConnector"]
