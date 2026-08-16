"""The report lifecycle: draft, select, justify, submit, export.

Submission is the only moment anything from a mailbox becomes durable. Before
it, a report is a list of message identifiers and some typed justifications;
after it, the selected receipts exist as files in artifact storage with their
hashes recorded, and the report is frozen.

Two rules make the frozen state meaningful:

* **Every item must carry a justification.** Chasing them afterwards is the
  administrative cost this whole application exists to remove, so submission
  refuses rather than producing a bundle that will need follow-up.
* **Artifacts are materialised before the status changes.** If any item fails to
  produce a file, nothing is committed and the report stays a draft. A
  half-submitted report that looks finished is worse than one that plainly failed.
"""

from __future__ import annotations

import datetime as _dt
import logging
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth.allowlist import ROLE_ADMIN, audit
from .connectors.base import MailConnector, MessageHeader
from .errors import CCReportError, RenderError
from .models import Artifact, MailAccount, Report, ReportItem, User
from .period import clamp_period, format_period, parse_period, period_label
from .render import RenderedDocument, normalize_attachment, render_message_to_pdf
from .settings import Settings, get_settings
from .storage import ArtifactStore, artifact_path, get_artifact_store, safe_filename

logger = logging.getLogger("ccreport.reports")

STATUS_DRAFT = "draft"
STATUS_SUBMITTED = "submitted"

#: How a caller hands us a connector for one account. Passed in rather than
#: built here so tests, the CLI and the web app all share this module without
#: this module needing to know how credentials are stored.
ConnectorFactory = Callable[[MailAccount], MailConnector]


class ReportNotFound(CCReportError):
    """No report for that person and month."""


class ReportStateError(CCReportError):
    """The report is not in a state where this makes sense."""


class ItemNotFound(CCReportError):
    """No such item in this report."""


# ------------------------------------------------------------------ lifecycle
def list_reports(session: Session, user: User) -> list[Report]:
    return list(
        session.scalars(
            select(Report)
            .where(Report.user_id == user.id)
            .order_by(Report.period_year.desc(), Report.period_month.desc())
        ).all()
    )


def find_report(session: Session, user: User, period: str) -> Report | None:
    year, month = parse_period(period)
    return session.scalar(
        select(Report).where(
            Report.user_id == user.id,
            Report.period_year == year,
            Report.period_month == month,
        )
    )


def get_report(session: Session, user: User, period: str) -> Report:
    report = find_report(session, user, period)
    if report is None:
        raise ReportNotFound(f"no report for {period}. Create one with `report create --month {period}`.")
    return report


def create_report(
    session: Session,
    user: User,
    period: str,
    *,
    title: str | None = None,
    settings: Settings | None = None,
    now: _dt.datetime | None = None,
) -> Report:
    """Start a draft for one month, or return the draft that already exists."""
    settings = settings or get_settings()
    year, month = clamp_period(period, settings.month_window, now)
    existing = find_report(session, user, format_period(year, month))
    if existing is not None:
        return existing

    report = Report(
        user_id=user.id,
        period_year=year,
        period_month=month,
        title=title or f"Receipts — {period_label(year, month)}",
    )
    session.add(report)
    session.flush()
    audit(
        session,
        actor=user.upn,
        action="report.create",
        subject_type="report",
        subject_id=report.id,
        detail={"period": report.period},
    )
    return report


def _require_draft(report: Report) -> None:
    if report.status != STATUS_DRAFT:
        raise ReportStateError(
            f"{report.period} was submitted on "
            f"{report.submitted_at.isoformat() if report.submitted_at else 'an earlier date'} "
            "and can no longer be changed."
        )


def add_item(
    session: Session,
    user: User,
    report: Report,
    account: MailAccount,
    header: MessageHeader,
) -> ReportItem:
    """Select one browsed message for the report.

    This is the first moment anything about a message is written down, and it
    writes the minimum: who sent it, when, what it was called, and the vendor
    and amount hints that let an administrator match it to a Concur line. The
    body is not copied — it becomes an artifact at submission or not at all.
    """
    _require_draft(report)
    existing = session.scalar(
        select(ReportItem).where(
            ReportItem.report_id == report.id,
            ReportItem.provider_message_id == header.id,
        )
    )
    if existing is not None:
        return existing

    position = 1 + max((i.position for i in report.items), default=0)
    item = ReportItem(
        report_id=report.id,
        source_account_id=account.id,
        position=position,
        provider=account.provider,
        provider_message_id=header.id,
        source_address=account.address,
        message_subject=header.subject or None,
        message_from=header.from_address,
        message_from_name=header.from_name,
        message_date=header.received_at,
        vendor_hint=header.vendor_hint,
        amount_hint_cents=header.amount_hint_cents,
        currency_hint=header.currency_hint,
        receipt_score=header.receipt_score,
        artifact_kind="attachment" if header.receipt_media_refs else "message_pdf",
    )
    session.add(item)
    session.flush()
    session.refresh(report)
    audit(
        session,
        actor=user.upn,
        action="report.add_item",
        subject_type="report_item",
        subject_id=item.id,
        detail={"period": report.period, "provider": account.provider, "message": header.id},
    )
    return item


def get_item(report: Report, reference: str | int) -> ReportItem:
    """Find an item by its 1-based position or by its id."""
    items = sorted(report.items, key=lambda i: (i.position, i.created_at))
    if isinstance(reference, int) or str(reference).isdigit():
        index = int(reference)
        if 1 <= index <= len(items):
            return items[index - 1]
        raise ItemNotFound(f"item {index} does not exist; the report has {len(items)} item(s)")
    for item in items:
        if item.id == reference:
            return item
    raise ItemNotFound(f"no item {reference!r} in this report")


def justify_item(
    session: Session, user: User, report: Report, reference: str | int, text: str
) -> ReportItem:
    _require_draft(report)
    text = text.strip()
    if not text:
        raise ReportStateError("a justification cannot be empty")
    item = get_item(report, reference)
    item.justification = text
    session.flush()
    audit(
        session,
        actor=user.upn,
        action="report.justify",
        subject_type="report_item",
        subject_id=item.id,
        detail={"period": report.period, "length": len(text)},
    )
    return item


def remove_item(session: Session, user: User, report: Report, reference: str | int) -> str:
    _require_draft(report)
    item = get_item(report, reference)
    item_id = item.id
    session.delete(item)
    session.flush()
    session.refresh(report)
    for position, remaining in enumerate(
        sorted(report.items, key=lambda i: (i.position, i.created_at)), 1
    ):
        remaining.position = position
    session.flush()
    audit(
        session,
        actor=user.upn,
        action="report.remove_item",
        subject_type="report_item",
        subject_id=item_id,
        detail={"period": report.period},
    )
    return item_id


# ----------------------------------------------------------------- submission
def _header_for_render(item: ReportItem) -> MessageHeader:
    """Rebuild just enough header for the PDF's provenance block."""
    return MessageHeader(
        id=item.provider_message_id,
        account_id=item.source_address,
        subject=item.message_subject or "",
        from_name=item.message_from_name,
        from_address=item.message_from,
        received_at=item.message_date,
        vendor_hint=item.vendor_hint,
        amount_hint_cents=item.amount_hint_cents,
        currency_hint=item.currency_hint,
        receipt_score=item.receipt_score,
    )


def _store_artifact(
    session: Session,
    store: ArtifactStore,
    report: Report,
    item: ReportItem,
    *,
    content: bytes,
    filename: str,
    content_type: str,
    kind: str,
    renderer: str | None = None,
    page_count: int | None = None,
    sha256: str | None = None,
) -> Artifact:
    import hashlib

    artifact = Artifact(
        item_id=item.id,
        blob_path="",
        filename=safe_filename(filename),
        content_type=content_type,
        size_bytes=len(content),
        sha256=sha256 or hashlib.sha256(content).hexdigest(),
        page_count=page_count,
        kind=kind,
        renderer=renderer,
    )
    session.add(artifact)
    session.flush()
    artifact.blob_path = artifact_path(report.id, artifact.id, artifact.filename)
    store.put(artifact.blob_path, content, content_type=content_type)
    session.flush()
    return artifact


def materialize_item(
    session: Session,
    report: Report,
    item: ReportItem,
    connector: MailConnector,
    *,
    store: ArtifactStore,
    settings: Settings,
) -> list[Artifact]:
    """Turn one selected message into the file(s) that will go in the bundle.

    Attachments win when there are any: an original PDF from a vendor is better
    evidence than our rendering of the email that carried it. Only when there is
    nothing attached do we render the message itself.
    """
    produced: list[Artifact] = []
    refs = [r for r in connector.attachment_refs(item.provider_message_id) if r.is_probably_receipt_media]

    for ref in refs:
        attachment = connector.fetch_attachment(item.provider_message_id, ref.id)
        try:
            content, filename, content_type = normalize_attachment(attachment, settings)
        except ValueError as exc:
            # An oversized or unrenderable attachment must not sink the whole
            # submission; the message is rendered instead and the reason is logged.
            logger.warning("attachment %s on %s skipped: %s", ref.filename, item.id, exc)
            continue
        produced.append(
            _store_artifact(
                session,
                store,
                report,
                item,
                content=content,
                filename=filename,
                content_type=content_type,
                kind="converted_image" if filename != ref.filename else "original_attachment",
            )
        )

    if produced:
        item.artifact_kind = "attachment"
        return produced

    body = connector.fetch_body(item.provider_message_id)
    document: RenderedDocument = render_message_to_pdf(
        body,
        _header_for_render(item),
        settings=settings,
        account_address=item.source_address,
    )
    item.artifact_kind = "message_pdf"
    produced.append(
        _store_artifact(
            session,
            store,
            report,
            item,
            content=document.content,
            filename=f"{item.message_subject or 'message'}.pdf",
            content_type="application/pdf",
            kind="rendered_message",
            renderer=document.renderer,
            page_count=document.page_count,
            sha256=document.sha256,
        )
    )
    return produced


def submit_report(
    session: Session,
    user: User,
    report: Report,
    *,
    open_connector: ConnectorFactory,
    store: ArtifactStore | None = None,
    settings: Settings | None = None,
    require_justifications: bool = True,
) -> Report:
    """Freeze the report and build its artifacts.

    Raises before changing anything if the report is not ready. On a failure
    part-way through, blobs already written are removed so a retry does not
    accumulate orphans; the caller's transaction is expected to roll back.
    """
    settings = settings or get_settings()
    store = store or get_artifact_store(settings)
    _require_draft(report)

    items = sorted(report.items, key=lambda i: (i.position, i.created_at))
    if not items:
        raise ReportStateError(f"{report.period} has no selected receipts to submit")

    if require_justifications:
        missing = [i.position for i in items if not (i.justification or "").strip()]
        if missing:
            listed = ", ".join(str(m) for m in missing)
            raise ReportStateError(
                f"item(s) {listed} have no justification. Every receipt needs one "
                "before the report can be submitted."
            )

    connectors: dict[str, MailConnector] = {}
    written: list[str] = []
    try:
        for item in items:
            if item.source_account_id is None:
                raise ReportStateError(
                    f"item {item.position} came from a mailbox that is no longer "
                    "connected; reconnect it or remove the item."
                )
            account = session.get(MailAccount, item.source_account_id)
            if account is None:
                raise ReportStateError(
                    f"item {item.position} came from a mailbox that is no longer "
                    "connected; reconnect it or remove the item."
                )
            if account.id not in connectors:
                connectors[account.id] = open_connector(account)
            artifacts = materialize_item(
                session, report, item, connectors[account.id], store=store, settings=settings
            )
            written.extend(a.blob_path for a in artifacts)
    except (RenderError, CCReportError, ValueError):
        for path in written:
            try:
                store.delete(path)
            except Exception:  # pragma: no cover - cleanup is best effort
                logger.warning("could not remove orphaned artifact %s", path)
        raise

    report.status = STATUS_SUBMITTED
    report.submitted_at = _dt.datetime.now(_dt.UTC)
    session.flush()
    audit(
        session,
        actor=user.upn,
        action="report.submit",
        subject_type="report",
        subject_id=report.id,
        detail={"period": report.period, "items": len(items), "artifacts": len(written)},
    )
    return report


def export_bundle(
    session: Session,
    report: Report,
    owner: User,
    *,
    store: ArtifactStore | None = None,
    settings: Settings | None = None,
    actor: User | None = None,
) -> tuple[str, bytes]:
    """Build the ZIP for one report. Returns ``(filename, bytes)``."""
    from .bundle import build_bundle, bundle_filename

    settings = settings or get_settings()
    store = store or get_artifact_store(settings)
    content = build_bundle(report, owner, store)
    filename = bundle_filename(owner.upn, report.period)
    audit(
        session,
        actor=(actor or owner).upn,
        action="report.export",
        subject_type="report",
        subject_id=report.id,
        detail={"period": report.period, "bytes": len(content), "owner": owner.upn},
    )
    return filename, content


# ---------------------------------------------------------------------- views
def item_summary(item: ReportItem, position: int | None = None) -> dict:
    from .bundle import format_amount

    return {
        "index": position if position is not None else item.position,
        "id": item.id,
        "provider": item.provider,
        "account": item.source_address,
        "message_id": item.provider_message_id,
        "subject": item.message_subject,
        "from": item.message_from,
        "from_name": item.message_from_name,
        "date": item.message_date.isoformat() if item.message_date else None,
        "vendor": item.vendor_hint,
        "amount": format_amount(item.amount_hint_cents, item.currency_hint),
        "amount_cents": item.amount_hint_cents,
        "currency": item.currency_hint,
        "justification": item.justification,
        "receipt_score": item.receipt_score,
        "artifact_kind": item.artifact_kind,
        "artifacts": [
            {
                "filename": a.filename,
                "content_type": a.content_type,
                "bytes": a.size_bytes,
                "sha256": a.sha256,
                "kind": a.kind,
                "renderer": a.renderer,
                "pages": a.page_count,
            }
            for a in sorted(item.artifacts, key=lambda a: a.created_at)
        ],
    }


def report_summary(report: Report, *, include_items: bool = True) -> dict:
    items = sorted(report.items, key=lambda i: (i.position, i.created_at))
    data = {
        "id": report.id,
        "period": report.period,
        "period_label": period_label(report.period_year, report.period_month),
        "title": report.title,
        "status": report.status,
        "notes": report.notes,
        "items": len(items),
        "unjustified": sum(1 for i in items if not (i.justification or "").strip()),
        "artifacts": sum(len(i.artifacts) for i in items),
        "submitted_at": report.submitted_at.isoformat() if report.submitted_at else None,
        "created_at": report.created_at.isoformat() if report.created_at else None,
    }
    if include_items:
        data["receipts"] = [item_summary(item, position) for position, item in enumerate(items, 1)]
    return data


def empty_report_summary(period: str) -> dict:
    """The shape of a month nobody has started yet.

    Lets a browse page render before a report exists, so viewing a month stays a
    read: a draft is created when the first receipt is selected, not when
    somebody clicks a link.
    """
    year, month = parse_period(period)
    return {
        "id": None,
        "period": format_period(year, month),
        "period_label": period_label(year, month),
        "title": f"Receipts — {period_label(year, month)}",
        "status": STATUS_DRAFT,
        "notes": None,
        "items": 0,
        "unjustified": 0,
        "artifacts": 0,
        "submitted_at": None,
        "created_at": None,
        "receipts": [],
    }


def admin_list_reports(
    session: Session,
    actor: User,
    *,
    upn: str | None = None,
    period: str | None = None,
    include_drafts: bool = False,
) -> list[tuple[User, Report]]:
    """Every submitted report, for an administrator.

    Drafts are excluded by default: an unsubmitted report is a faculty member's
    working state, and reading it would make this a surveillance tool rather
    than a handoff.
    """
    if actor.role != ROLE_ADMIN:
        from .auth.allowlist import InsufficientRole

        raise InsufficientRole(f"{actor.upn} does not hold the 'admin' role required for this action.")

    stmt = select(User, Report).join(Report, Report.user_id == User.id)
    if not include_drafts:
        stmt = stmt.where(Report.status == STATUS_SUBMITTED)
    if upn:
        stmt = stmt.where(User.upn == upn.strip().lower())
    if period:
        year, month = parse_period(period)
        stmt = stmt.where(Report.period_year == year, Report.period_month == month)
    stmt = stmt.order_by(Report.period_year.desc(), Report.period_month.desc(), User.upn)
    return [(u, r) for u, r in session.execute(stmt).all()]


__all__ = [
    "STATUS_DRAFT",
    "STATUS_SUBMITTED",
    "ItemNotFound",
    "ReportNotFound",
    "ReportStateError",
    "add_item",
    "admin_list_reports",
    "create_report",
    "empty_report_summary",
    "export_bundle",
    "find_report",
    "get_item",
    "get_report",
    "item_summary",
    "justify_item",
    "list_reports",
    "materialize_item",
    "remove_item",
    "report_summary",
    "submit_report",
]
