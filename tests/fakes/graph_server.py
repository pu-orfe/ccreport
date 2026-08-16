from __future__ import annotations

from urllib.parse import parse_qs

import httpx

from .fixtures import VENDOR_HTML_RECEIPT


class FakeGraph:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.fail_auth = False
        self.throttle_once = False
        self._throttled = False

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle), base_url="https://graph.microsoft.com")

    def _json(self, data: dict, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json=data)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_auth:
            return self._json({"error": "invalid"}, 401)
        if self.throttle_once and not self._throttled:
            self._throttled = True
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "slow"})
        path = request.url.path
        query = parse_qs(request.url.query.decode())
        if path == "/v1.0/me":
            return self._json({"mail": "ada@example.edu"})
        if path == "/v1.0/me/mailFolders":
            return self._json({"value": [{"id": "inbox", "displayName": "Inbox", "totalItemCount": 2, "unreadItemCount": 0, "childFolderCount": 1}, {"id": "archive", "displayName": "Archive"}]})
        if path == "/v1.0/me/mailFolders/inbox/childFolders":
            return self._json({"value": [{"id": "receipts", "displayName": "Receipts"}]})
        if path in {"/v1.0/me/messages", "/v1.0/me/mailFolders/inbox/messages"}:
            if "page=2" in str(request.url):
                return self._json({"value": [self._message("m2", "No attachment", False)]})
            return self._json({"value": [self._message("m1", "Bob's receipt", True)], "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/messages?page=2"})
        if path == "/v1.0/me/messages/m1/attachments":
            return self._json({"value": [{"id": "a1", "name": "receipt.pdf", "contentType": "application/pdf", "size": 12, "isInline": False}, {"id": "inline", "name": "pixel.png", "contentType": "image/png", "size": 1, "isInline": True, "contentId": "cid"}, {"id": "ref", "name": "link", "@odata.type": "#microsoft.graph.referenceAttachment"}]})
        if path == "/v1.0/me/messages/m1/attachments/a1/$value":
            return httpx.Response(200, content=b"pdf-bytes")
        if path == "/v1.0/me/messages/m1/$value":
            return httpx.Response(200, content=VENDOR_HTML_RECEIPT)
        if path == "/v1.0/me/messages/m1" and query.get("$select") == ["body"]:
            return self._json({"body": {"contentType": "html", "content": "<p>Receipt</p>"}})
        return self._json({"error": path}, 404)

    def _message(self, mid: str, subject: str, has_attachments: bool) -> dict:
        return {"id": mid, "subject": subject, "from": {"emailAddress": {"name": "Vendor", "address": "sales@example.com"}}, "toRecipients": [{"emailAddress": {"address": "ada@example.edu"}}], "receivedDateTime": "2026-07-05T12:00:00Z", "hasAttachments": has_attachments, "bodyPreview": "preview", "webLink": "https://example.com"}
