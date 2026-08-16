from __future__ import annotations

import datetime as _dt
import email.utils
import time
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
)

_GRAPH = "https://graph.microsoft.com/v1.0"
_WELL_KNOWN = {"inbox", "archive", "sent", "sent items", "deleted items", "junk email", "drafts"}


def _odata_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _iso(value: _dt.datetime) -> str:
    return value.astimezone(_dt.UTC).isoformat().replace("+00:00", "Z")


def _parse_dt(value: str | None) -> _dt.datetime | None:
    if not value:
        return None
    return _dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(_dt.UTC)


class GraphConnector:
    provider = "graph"

    def __init__(self, access_token: str, *, http: httpx.Client | None = None, address: str | None = None):
        self._token = access_token
        self._http = http or shared_client()
        self._address = address

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        if url.startswith("/"):
            url = f"{_GRAPH}{url}"
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self._token}"
        last: httpx.Response | None = None
        for attempt in range(4):
            resp = self._http.request(method, url, headers=headers, **kwargs)
            if resp.status_code == 401:
                raise ReauthRequired("Microsoft Graph credential is no longer valid; reconnect this account.")
            if resp.status_code not in {429, 503}:
                if resp.status_code >= 400:
                    raise ConnectorError(f"Microsoft Graph returned HTTP {resp.status_code}: {resp.text}")
                return resp
            last = resp
            if attempt < 3:
                retry = resp.headers.get("Retry-After")
                delay = 0.1
                if retry:
                    try:
                        delay = min(float(retry), 1.0)
                    except ValueError:
                        parsed = email.utils.parsedate_to_datetime(retry)
                        delay = min(max((parsed - _dt.datetime.now(_dt.UTC)).total_seconds(), 0), 1.0)
                time.sleep(delay)
        raise ConnectorError(f"Microsoft Graph throttled the request after retries: HTTP {last.status_code if last else 'unknown'}")

    def status(self) -> ConnectorStatus:
        try:
            data = self._request("GET", "/me", params={"$select": "mail,userPrincipalName"}).json()
        except ReauthRequired:
            raise
        address = self._address or data.get("mail") or data.get("userPrincipalName")
        return ConnectorStatus(ok=True, provider=self.provider, address=address, detail="Microsoft Graph reachable")

    def folders(self) -> list[MailFolder]:
        out: list[MailFolder] = []

        def visit(url: str, parent: str | None = None) -> None:
            while url:
                data = self._request("GET", url).json()
                for item in data.get("value", []):
                    name = item.get("displayName") or item.get("name") or item.get("id", "")
                    path = f"{parent}/{name}" if parent else name
                    out.append(
                        MailFolder(
                            id=item["id"],
                            name=name,
                            path=path,
                            total_count=item.get("totalItemCount"),
                            unread_count=item.get("unreadItemCount"),
                            well_known=name.lower() in _WELL_KNOWN,
                        )
                    )
                    child_count = item.get("childFolderCount") or 0
                    if child_count:
                        visit(f"/me/mailFolders/{quote(item['id'])}/childFolders", path)
                url = data.get("@odata.nextLink") or ""

        visit("/me/mailFolders?$top=100")
        return out

    def search(self, query: MessageQuery) -> list[MessageHeader]:
        folders = query.folder_ids or (None,)
        results: list[MessageHeader] = []
        for folder_id in folders:
            results.extend(self._search_folder(folder_id, query, query.limit - len(results)))
            if len(results) >= query.limit:
                break
        return results[: query.limit]

    def _search_folder(self, folder_id: str | None, query: MessageQuery, limit: int) -> list[MessageHeader]:
        if limit <= 0:
            return []
        filters = [f"receivedDateTime ge {_iso(query.start)}", f"receivedDateTime lt {_iso(query.end)}"]
        if query.has_attachments_only:
            filters.append("hasAttachments eq true")
        # Graph cannot combine $search with $filter/$orderby. Date-bounded browsing is mandatory,
        # so header narrowing stays client-side rather than issuing an invalid request shape.
        params = {
            "$filter": " and ".join(filters),
            "$select": "id,subject,from,toRecipients,receivedDateTime,hasAttachments,bodyPreview,webLink",
            "$orderby": "receivedDateTime desc",
            "$top": str(min(limit, 100)),
        }
        path = "/me/messages" if folder_id is None else f"/me/mailFolders/{quote(folder_id)}/messages"
        url: str | None = path
        out: list[MessageHeader] = []
        first = True
        while url and len(out) < limit:
            resp = self._request("GET", url, params=params if first else None).json()
            first = False
            for item in resp.get("value", []):
                header = self._header(item, folder_id)
                if self._matches(header, query):
                    out.append(header)
                    if len(out) >= limit:
                        break
            url = resp.get("@odata.nextLink")
        return out

    def _matches(self, header: MessageHeader, query: MessageQuery) -> bool:
        if query.subject_contains and query.subject_contains.lower() not in header.subject.lower():
            return False
        if query.from_contains:
            needle = query.from_contains.lower()
            hay = " ".join(x or "" for x in [header.from_name, header.from_address]).lower()
            if needle not in hay:
                return False
        return True

    def _header(self, item: dict, folder_id: str | None) -> MessageHeader:
        sender = ((item.get("from") or {}).get("emailAddress") or {})
        to = [
            (r.get("emailAddress") or {}).get("address", "").lower()
            for r in item.get("toRecipients", [])
            if (r.get("emailAddress") or {}).get("address")
        ]
        return MessageHeader(
            id=item["id"],
            account_id=self._address,
            folder_id=folder_id,
            subject=item.get("subject") or "",
            from_name=sender.get("name"),
            from_address=(sender.get("address") or "").lower() or None,
            to=to,
            received_at=_parse_dt(item.get("receivedDateTime")),
            has_attachments=bool(item.get("hasAttachments")),
            attachment_refs=self.attachment_refs(item["id"]) if item.get("hasAttachments") else [],
            snippet=item.get("bodyPreview") or "",
            web_link=item.get("webLink"),
        )

    def attachment_refs(self, message_id: str) -> list[AttachmentRef]:
        data = self._request(
            "GET",
            f"/me/messages/{quote(message_id)}/attachments",
            params={"$select": "id,name,contentType,size,isInline,contentId"},
        ).json()
        refs: list[AttachmentRef] = []
        for item in data.get("value", []):
            otype = item.get("@odata.type", "#microsoft.graph.fileAttachment")
            if "itemAttachment" in otype or "referenceAttachment" in otype:
                continue
            refs.append(
                AttachmentRef(
                    id=item["id"],
                    filename=item.get("name") or item["id"],
                    content_type=item.get("contentType"),
                    size_bytes=item.get("size"),
                    inline=bool(item.get("isInline")),
                    content_id=item.get("contentId"),
                )
            )
        return refs

    def fetch_body(self, message_id: str) -> MessageBody:
        raw = self._request("GET", f"/me/messages/{quote(message_id)}/$value").content
        data = self._request("GET", f"/me/messages/{quote(message_id)}", params={"$select": "body"}).json()
        body = data.get("body") or {}
        return MessageBody(id=message_id, html=body.get("content") if body.get("contentType") == "html" else None, text=body.get("content") if body.get("contentType") == "text" else None, mime=raw)

    def fetch_attachment(self, message_id: str, attachment_id: str) -> Attachment:
        refs = {r.id: r for r in self.attachment_refs(message_id)}
        ref = refs.get(attachment_id) or AttachmentRef(attachment_id, attachment_id, None, None)
        content = self._request("GET", f"/me/messages/{quote(message_id)}/attachments/{quote(attachment_id)}/$value").content
        return Attachment(ref=ref, content=content)


__all__ = ["GraphConnector", "_odata_string_literal"]
