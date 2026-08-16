"""The ZIP bundle: what an administrator actually receives.

One report becomes one file with five things in it, and the shape of all five is
fixed so that the twentieth bundle of the month looks exactly like the first:

``manifest.json``
    Schema-versioned. Every item, artifact, hash and justification.
``summary.csv``
    Opens in Excel without an import wizard. One row per receipt.
``index.html``
    A printable contact sheet, for the administrator who works on paper.
``ccworks-apply.json``
    Input for ``ccworks report apply-json``.
``receipts/NNN-….pdf``
    Zero-padded, collision-safe, ordered the way the report is ordered.

``ccworks-apply.json`` deliberately omits ``index``. Concur row indices are
positional and change as rows are added; carrying vendor and amount instead is
what lets ``apply-json`` refuse to attach a receipt to the wrong expense rather
than attaching it to whatever now sits at row 4.
"""

from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath

from .models import Report, ReportItem, User
from .period import period_label
from .storage import ArtifactStore, safe_filename

#: Bumped when the manifest's shape changes in a way a consumer must notice.
BUNDLE_SCHEMA_VERSION = 1
APPLY_SCHEMA_VERSION = 1

_ZERO_MINOR = {"JPY"}
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def format_amount(cents: int | None, currency: str | None) -> str | None:
    if cents is None:
        return None
    if (currency or "").upper() in _ZERO_MINOR:
        return str(cents)
    sign = "-" if cents < 0 else ""
    whole, minor = divmod(abs(cents), 100)
    return f"{sign}{whole}.{minor:02d}"


def slugify(value: str, *, default: str = "receipt", max_length: int = 40) -> str:
    slug = _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-")
    return (slug[:max_length].strip("-") or default)


def bundle_filename(upn: str, period: str) -> str:
    """``ccreport-<user>-<YYYY-MM>.zip``, with the local part of the UPN."""
    local = (upn or "user").split("@", 1)[0]
    return f"ccreport-{slugify(local, default='user')}-{period}.zip"


@dataclass(slots=True)
class BundleEntry:
    """One artifact, and the name it will carry inside the ZIP."""

    item: ReportItem
    index: int
    path: str
    content: bytes
    content_type: str
    sha256: str
    kind: str
    renderer: str | None
    original_filename: str


def _entry_name(index: int, item: ReportItem, original: str, taken: set[str]) -> str:
    suffix = PurePosixPath(safe_filename(original)).suffix or ".pdf"
    stem = slugify(item.vendor_hint or item.message_subject or "receipt")
    candidate = f"receipts/{index:03d}-{stem}{suffix}"
    disambiguator = 2
    while candidate in taken:
        candidate = f"receipts/{index:03d}-{stem}-{disambiguator}{suffix}"
        disambiguator += 1
    taken.add(candidate)
    return candidate


def collect_entries(report: Report, store: ArtifactStore) -> list[BundleEntry]:
    """Read every artifact out of storage and give it its place in the bundle."""
    entries: list[BundleEntry] = []
    taken: set[str] = set()
    index = 0
    for item in sorted(report.items, key=lambda i: (i.position, i.created_at)):
        index += 1
        for artifact in sorted(item.artifacts, key=lambda a: a.created_at):
            content = store.get(artifact.blob_path)
            entries.append(
                BundleEntry(
                    item=item,
                    index=index,
                    path=_entry_name(index, item, artifact.filename, taken),
                    content=content,
                    content_type=artifact.content_type,
                    sha256=hashlib.sha256(content).hexdigest(),
                    kind=artifact.kind,
                    renderer=artifact.renderer,
                    original_filename=artifact.filename,
                )
            )
    return entries


def build_manifest(
    report: Report, user: User, entries: list[BundleEntry], *, generated_at: _dt.datetime
) -> dict:
    by_item: dict[str, list[BundleEntry]] = {}
    for entry in entries:
        by_item.setdefault(entry.item.id, []).append(entry)

    items: list[dict] = []
    total_cents = 0
    currencies: set[str] = set()
    for position, item in enumerate(sorted(report.items, key=lambda i: (i.position, i.created_at)), 1):
        item_entries = by_item.get(item.id, [])
        if item.amount_hint_cents is not None:
            total_cents += item.amount_hint_cents
            currencies.add((item.currency_hint or "USD").upper())
        items.append(
            {
                "index": position,
                "vendor": item.vendor_hint,
                "amount": format_amount(item.amount_hint_cents, item.currency_hint),
                "amount_cents": item.amount_hint_cents,
                "currency": item.currency_hint,
                "date": item.message_date.isoformat() if item.message_date else None,
                "subject": item.message_subject,
                "from": item.message_from,
                "from_name": item.message_from_name,
                "justification": item.justification,
                "receipt_score": item.receipt_score,
                "artifact_kind": item.artifact_kind,
                "source": {
                    "provider": item.provider,
                    "account": item.source_address,
                    "message_id": item.provider_message_id,
                },
                "artifacts": [
                    {
                        "file": entry.path,
                        "original_filename": entry.original_filename,
                        "content_type": entry.content_type,
                        "bytes": len(entry.content),
                        "sha256": entry.sha256,
                        "kind": entry.kind,
                        "renderer": entry.renderer,
                    }
                    for entry in item_entries
                ],
            }
        )

    single_currency = currencies.copy().pop() if len(currencies) == 1 else None
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "generator": "ccreport",
        "generated_at": generated_at.isoformat(),
        "user": {"upn": user.upn, "display_name": user.display_name},
        "period": report.period,
        "period_label": period_label(report.period_year, report.period_month),
        "items": items,
        "report": {
            "id": report.id,
            "title": report.title,
            "status": report.status,
            "notes": report.notes,
            "submitted_at": report.submitted_at.isoformat() if report.submitted_at else None,
        },
        "totals": {
            "items": len(items),
            "artifacts": len(entries),
            "amount_cents": total_cents,
            "amount": format_amount(total_cents, single_currency) if single_currency else None,
            # One currency is the overwhelmingly common case; more than one is
            # stated rather than silently summed into a meaningless number.
            "currency": single_currency,
            "currencies": sorted(currencies),
        },
    }


def build_summary_csv(manifest: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["date", "vendor", "amount", "currency", "justification", "filename"])
    for item in manifest["items"]:
        files = "; ".join(a["file"] for a in item["artifacts"])
        writer.writerow(
            [
                (item["date"] or "")[:10],
                item["vendor"] or "",
                item["amount"] or "",
                item["currency"] or "",
                item["justification"] or "",
                files,
            ]
        )
    return buffer.getvalue()


def build_apply_json(manifest: dict) -> dict:
    """The ``ccworks report apply-json`` payload.

    No ``index`` field, by design. See the module docstring and
    ``docs/CCWORKS-HANDOFF.md``.
    """
    receipts = []
    for item in manifest["items"]:
        for artifact in item["artifacts"]:
            receipts.append(
                {
                    "file": artifact["file"],
                    "vendor": item["vendor"],
                    "amount": item["amount"],
                    "currency": item["currency"],
                    "date": (item["date"] or "")[:10] or None,
                    "justification": item["justification"],
                    "sha256": artifact["sha256"],
                }
            )
    return {
        "schema_version": APPLY_SCHEMA_VERSION,
        "generated_by": "ccreport",
        "period": manifest["period"],
        "user": manifest["user"]["upn"],
        "receipts": receipts,
    }


def build_index_html(manifest: dict) -> str:
    from jinja2 import Environment, PackageLoader, select_autoescape

    env = Environment(
        loader=PackageLoader("ccreport", "templates"),
        autoescape=select_autoescape(["html"]),
    )
    return env.get_template("bundle_index.html").render(manifest=manifest)


def build_bundle(
    report: Report,
    user: User,
    store: ArtifactStore,
    *,
    generated_at: _dt.datetime | None = None,
) -> bytes:
    """Assemble the whole bundle in memory and return the ZIP bytes."""
    generated_at = generated_at or _dt.datetime.now(_dt.UTC)
    entries = collect_entries(report, store)
    manifest = build_manifest(report, user, entries, generated_at=generated_at)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=False) + "\n")
        archive.writestr("summary.csv", build_summary_csv(manifest))
        archive.writestr("index.html", build_index_html(manifest))
        archive.writestr(
            "ccworks-apply.json", json.dumps(build_apply_json(manifest), indent=2) + "\n"
        )
        for entry in entries:
            archive.writestr(entry.path, entry.content)
    return buffer.getvalue()


__all__ = [
    "APPLY_SCHEMA_VERSION",
    "BUNDLE_SCHEMA_VERSION",
    "BundleEntry",
    "build_apply_json",
    "build_bundle",
    "build_index_html",
    "build_manifest",
    "build_summary_csv",
    "bundle_filename",
    "collect_entries",
    "format_amount",
    "slugify",
]
