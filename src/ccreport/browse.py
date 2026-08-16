"""Browsing a month of a mailbox.

The one operation faculty perform most, and the one that must leave no trace.
Headers come from the provider, are scored by :mod:`ccreport.filters`, and are
held in the in-process cache. Nothing on this path opens a write transaction —
``tests/integration`` asserts exactly that against real PostgreSQL.

``receipts_only`` filters the *returned* list, never the cached one. Highlighting
is a view, so toggling it must not cost another round trip to the provider, and
the highlight must never be able to hide a message that exists.
"""

from __future__ import annotations

import datetime as _dt
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from .cache import CacheKey, HeaderCache, get_header_cache
from .connectors.base import MailConnector, MessageHeader, MessageQuery
from .filters import header_matches, score_message
from .models import MailAccount, User
from .period import clamp_period, format_period, month_bounds
from .settings import Settings, get_settings

logger = logging.getLogger("ccreport.browse")

#: A month of a busy academic mailbox. Beyond this the browse view stops being
#: usable anyway, and the provider starts paging for a long time.
DEFAULT_LIMIT = 500


@dataclass(slots=True)
class BrowseResult:
    account_id: str
    address: str
    provider: str
    period: str
    headers: list[MessageHeader]
    total: int
    likely_receipts: int
    from_cache: bool
    folder_ids: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "account": {"id": self.account_id, "address": self.address, "provider": self.provider},
            "period": self.period,
            "total": self.total,
            "likely_receipts": self.likely_receipts,
            "from_cache": self.from_cache,
            "folders": list(self.folder_ids),
            "messages": [header_summary(h) for h in self.headers],
            "warnings": self.warnings,
        }


def header_summary(header: MessageHeader) -> dict:
    """One message, in the shape the CLI prints and the browse view renders."""
    return {
        "id": header.id,
        "subject": header.subject,
        "from": header.from_address,
        "from_name": header.from_name,
        "received_at": header.received_at.isoformat() if header.received_at else None,
        "has_attachments": header.has_attachments,
        "attachments": [
            {
                "id": ref.id,
                "filename": ref.filename,
                "content_type": ref.content_type,
                "size_bytes": ref.size_bytes,
                "inline": ref.inline,
                "receipt_media": ref.is_probably_receipt_media,
            }
            for ref in header.attachment_refs
        ],
        "snippet": header.snippet,
        "receipt_score": header.receipt_score,
        "likely_receipt": header.likely_receipt,
        "vendor_hint": header.vendor_hint,
        "amount_hint_cents": header.amount_hint_cents,
        "currency_hint": header.currency_hint,
        "signals": list(header.matched_signals),
        "web_link": header.web_link,
    }


def browse(
    session: Session,
    user: User,
    account: MailAccount,
    period: str,
    *,
    connector: MailConnector,
    folder_ids: tuple[str, ...] = (),
    subject_contains: str | None = None,
    from_contains: str | None = None,
    receipts_only: bool = False,
    has_attachments_only: bool = False,
    limit: int = DEFAULT_LIMIT,
    settings: Settings | None = None,
    cache: HeaderCache | None = None,
    vendors: Mapping[str, str] | None = None,
    now: _dt.datetime | None = None,
) -> BrowseResult:
    """List and score one month of one mailbox.

    ``session`` is taken but never written through. It is here so a caller
    cannot accidentally build a browse path that bypasses authorization, and so
    the signature does not change when auditing of browse is added.
    """
    settings = settings or get_settings()
    cache = cache or get_header_cache(settings)
    year, month = clamp_period(period, settings.month_window, now)
    start, end = month_bounds(year, month)

    key = CacheKey(
        user_id=user.id,
        account_id=account.id,
        period=format_period(year, month),
        folder_ids=tuple(folder_ids),
        subject_contains=subject_contains,
        from_contains=from_contains,
        has_attachments_only=has_attachments_only,
    )

    headers = cache.get(key)
    from_cache = headers is not None
    if headers is None:
        query = MessageQuery(
            start=start,
            end=end,
            folder_ids=tuple(folder_ids),
            subject_contains=subject_contains,
            from_contains=from_contains,
            has_attachments_only=has_attachments_only,
            limit=limit,
        )
        headers = connector.search(query)
        # Providers disagree about how faithfully they honour a header search:
        # Gmail does it server-side, Graph cannot combine it with a date filter,
        # IMAP is substring-based. Re-applying it here makes the three agree.
        headers = [
            score_message(h, vendors=vendors)
            for h in headers
            if header_matches(h, subject_contains=subject_contains, from_contains=from_contains)
        ]
        headers.sort(key=lambda h: (h.received_at or start), reverse=True)
        cache.put(key, headers)
        logger.debug(
            "browsed %s %s: %d headers from the provider", account.address, key.period, len(headers)
        )

    likely = sum(1 for h in headers if h.likely_receipt)
    visible = [h for h in headers if h.likely_receipt] if receipts_only else headers
    return BrowseResult(
        account_id=account.id,
        address=account.address,
        provider=account.provider,
        period=key.period,
        headers=visible,
        total=len(headers),
        likely_receipts=likely,
        from_cache=from_cache,
        folder_ids=tuple(folder_ids),
    )


def find_header(
    session: Session,
    user: User,
    account: MailAccount,
    period: str,
    message_id: str,
    *,
    connector: MailConnector,
    settings: Settings | None = None,
    cache: HeaderCache | None = None,
    now: _dt.datetime | None = None,
) -> MessageHeader | None:
    """Locate one browsed message again, so selecting it does not re-fetch a month."""
    result = browse(
        session,
        user,
        account,
        period,
        connector=connector,
        settings=settings,
        cache=cache,
        now=now,
    )
    for header in result.headers:
        if header.id == message_id:
            return header
    return None


def list_folders(connector: MailConnector) -> list[dict]:
    return [
        {
            "id": folder.id,
            "name": folder.name,
            "path": folder.display_path,
            "total": folder.total_count,
            "unread": folder.unread_count,
            "well_known": folder.well_known,
        }
        for folder in connector.folders()
    ]


__all__ = [
    "DEFAULT_LIMIT",
    "BrowseResult",
    "browse",
    "find_header",
    "header_summary",
    "list_folders",
]
