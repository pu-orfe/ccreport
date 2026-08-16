# The bundle and the `apply-json` contract

`ccreport` ends where [`ccworks`](https://github.com/pu-orfe/ccworks) begins. This is the
interface between them.

## The bundle

Submitting freezes a report and produces `ccreport-<user>-<YYYY-MM>.zip`:

```
manifest.json        schema-versioned; every item, artifact, hash and justification
summary.csv          date, vendor, amount, currency, justification, filename
index.html           printable contact sheet
ccworks-apply.json   input for `ccworks report apply-json`
receipts/001-….pdf   zero-padded, collision-safe, in report order
```

Receipt filenames are generated, never taken from the mailbox: the stem comes from the
vendor hint or subject, the number is the item's 1-based position, and a collision gets a
`-2` suffix. An attachment called `../../etc/passwd` becomes `002-w-b-mason.pdf`.

## `manifest.json`

```jsonc
{
  "schema_version": 1,
  "generated_at": "2026-08-15T00:00:00+00:00",
  "user": { "upn": "ada@princeton.edu", "display_name": "Ada Lovelace" },
  "period": "2026-07",
  "items": [
    {
      "index": 1,
      "vendor": "Amazon",
      "amount": "42.50",              // decimal string, formatted for the currency
      "amount_cents": 4250,           // integer minor units; the authoritative field
      "currency": "USD",
      "date": "2026-07-03T12:00:00+00:00",
      "subject": "Amazon.com order receipt",
      "from": "billing@vendor.example",
      "justification": "Textbooks for ORF 405",
      "artifact_kind": "attachment",  // attachment | message_pdf
      "source": { "provider": "graph", "account": "…", "message_id": "…" },
      "artifacts": [{
        "file": "receipts/001-amazon.pdf",
        "original_filename": "receipt.pdf",
        "content_type": "application/pdf",
        "bytes": 17,
        "sha256": "…",
        "kind": "original_attachment", // original_attachment | rendered_message | converted_image
        "renderer": null               // weasyprint | playwright | null
      }]
    }
  ],
  "report": { "id": "…", "status": "submitted", "submitted_at": "…" },
  "totals": {
    "items": 1, "artifacts": 1,
    "amount_cents": 4250, "amount": "42.50",
    "currency": "USD",                // null when the report mixes currencies
    "currencies": ["USD"]
  }
}
```

`schema_version` is bumped when a consumer must notice the change; adding an optional field
is not a bump. **`artifacts` is a list** — one message with three PDFs is one item with
three artifacts, and a consumer assuming one file per item will drop receipts. **Amounts
are hints**, extracted from message text: use them to *match* a charge, never to alter one.

## `ccworks-apply.json`

```jsonc
{
  "schema_version": 1,
  "period": "2026-07",
  "user": "ada@princeton.edu",
  "receipts": [{
    "file": "receipts/001-amazon.pdf",
    "vendor": "Amazon",
    "amount": "42.50",
    "currency": "USD",
    "date": "2026-07-03",
    "justification": "Textbooks for ORF 405",
    "sha256": "…"
  }]
}
```

One entry per **artifact**, so every file in the bundle has somewhere to go.

### Why there is no `index`

Concur row indices are positional. They move when a row is added, when a card feed imports
overnight, and when somebody re-sorts. An index captured at 09:00 and applied at 14:00 is a
reference to a position, not to an expense.

So the payload carries vendor, amount, currency and date instead, and `apply-json` is
expected to re-read the live report with `ccworks report show`, match, and **refuse the
ambiguous case**. A receipt attached to the wrong expense is worse than one not attached,
because nobody discovers it; an unmatched receipt is a minute of human work.

| Signal | Strength | Note |
|---|---|---|
| `amount` + `date` (±3 days) | strong | The pair a card feed preserves |
| `amount` alone | moderate | Fine when unique within the report |
| `vendor` substring | weak | Merchant strings differ between feed and email |
| `justification` | none | Never a match key |

`sha256` lets a re-run recognise a receipt it already attached and skip it.

## Compatibility

* UTF-8 throughout; `summary.csv` uses `\n` and a header row.
* Every path in the ZIP is relative with no `..` segment.
* `manifest.json` is the authority — the other three files are renderings or projections of
  it and carry nothing it does not.
* Absent optional values are `null`, never omitted and never `""`.
