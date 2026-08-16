from __future__ import annotations

import base64
from email.message import EmailMessage


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


VENDOR_HTML_RECEIPT = b"""From: Vendor <sales@example.com>
To: Ada <ada@example.edu>
Subject: Receipt
Date: Wed, 05 Jul 2026 12:00:00 +0000
Content-Type: text/html; charset=utf-8

<html><body>Receipt</body></html>
"""

MULTIPART_ALTERNATIVE = b"""From: Vendor <sales@example.com>
To: Ada <ada@example.edu>
Subject: Alt receipt
Date: Wed, 05 Jul 2026 12:00:00 +0000
Content-Type: multipart/alternative; boundary=alt

--alt
Content-Type: text/plain

Receipt
--alt
Content-Type: text/html

<b>Receipt</b>
--alt--
"""

RFC2047_SUBJECT = "=?utf-8?B?UmVjw6lwaXNzZQ==?="
RFC2047_MESSAGE = (
    f"From: Caf\xc3\xa9 <cafe@example.com>\r\nTo: Ada <ada@example.edu>\r\nSubject: {RFC2047_SUBJECT}\r\n"
    "Date: Wed, 05 Jul 2026 12:00:00 +0000\r\n\r\nThanks\r\n"
).encode()


def pdf_message() -> bytes:
    msg = EmailMessage()
    msg["From"] = "Vendor <sales@example.com>"
    msg["To"] = "Ada <ada@example.edu>"
    msg["Subject"] = "PDF receipt"
    msg["Date"] = "Wed, 05 Jul 2026 12:00:00 +0000"
    msg.set_content("Receipt attached")
    msg.add_attachment(b"%PDF-1.4 receipt", maintype="application", subtype="pdf", filename="receipt.pdf")
    return msg.as_bytes()
