from __future__ import annotations

from urllib.parse import parse_qs

import httpx

from .fixtures import VENDOR_HTML_RECEIPT, b64url


class FakeGmail:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.fail_auth = False
        self.rate_once = False
        self._rated = False

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self._handle), base_url="https://gmail.googleapis.com")

    def _json(self, data: dict, status: int = 200) -> httpx.Response:
        return httpx.Response(status, json=data)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_auth:
            return self._json({"error": "bad"}, 401)
        if self.rate_once and not self._rated:
            self._rated = True
            return self._json({"error": {"errors": [{"reason": "rateLimitExceeded"}]}}, 403)
        path = request.url.path
        qs = parse_qs(request.url.query.decode())
        if path == "/gmail/v1/users/me/profile":
            return self._json({"emailAddress": "ada@example.edu"})
        if path == "/gmail/v1/users/me/labels":
            return self._json({"labels": [{"id": "INBOX", "name": "INBOX"}, {"id": "Label_1", "name": "Receipts"}]})
        if path.endswith("/labels/INBOX"):
            return self._json({"id": "INBOX", "name": "INBOX", "type": "system", "messagesTotal": 2, "messagesUnread": 0})
        if path.endswith("/labels/Label_1"):
            return self._json({"id": "Label_1", "name": "Receipts", "type": "user", "messagesTotal": 1, "messagesUnread": 0})
        if path == "/gmail/v1/users/me/messages":
            if "pageToken" in qs:
                return self._json({"messages": [{"id": "gm2"}]})
            return self._json({"messages": [{"id": "gm1"}], "nextPageToken": "p2"})
        if path == "/gmail/v1/users/me/messages/gm1" and qs.get("format") == ["metadata"]:
            return self._json(self._metadata("gm1", "Bob's receipt"))
        if path == "/gmail/v1/users/me/messages/gm2" and qs.get("format") == ["metadata"]:
            return self._json(self._metadata("gm2", "No attachment", with_attachment=False))
        if path == "/gmail/v1/users/me/messages/gm1" and qs.get("format") == ["raw"]:
            return self._json({"raw": b64url(VENDOR_HTML_RECEIPT)})
        if path == "/gmail/v1/users/me/messages/gm1/attachments/att1":
            return self._json({"data": b64url(b"pdf-bytes")})
        return self._json({"error": path}, 404)

    def _metadata(self, mid: str, subject: str, *, with_attachment: bool = True) -> dict:
        parts = []
        if with_attachment:
            parts.append({"filename": "receipt.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "att1", "size": 9}, "headers": [{"name": "Content-Disposition", "value": "attachment; filename=receipt.pdf"}]})
            parts.append({"filename": "pixel.png", "mimeType": "image/png", "body": {"attachmentId": "inline1", "size": 1}, "headers": [{"name": "Content-Disposition", "value": "inline"}, {"name": "Content-ID", "value": "<cid>"}]})
        return {"id": mid, "snippet": "preview", "payload": {"headers": [{"name": "Subject", "value": subject}, {"name": "From", "value": "Vendor <sales@example.com>"}, {"name": "To", "value": "Ada <ada@example.edu>"}, {"name": "Date", "value": "Wed, 05 Jul 2026 12:00:00 +0000"}], "parts": parts}}
