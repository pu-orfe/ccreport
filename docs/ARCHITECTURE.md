# How the pieces fit, and why

Three constraints drive nearly every decision here:

1. **Faculty use it once a month, briefly, or not at all.** Anything that asks them to
   reconnect or re-learn will not happen, and the administrator goes back to chasing email.
2. **It reads mailboxes.** So the least possible mail becomes durable, credentials are
   never in the clear, and read-only is enforced by the shape of the code.
3. **Its output feeds a machine.** The bundle is a contract, not a convenience.

## The layers

```
Easy Auth ─▶ web/app.py  (server-rendered forms)   cli.py  (<group> <subcommand>, JSON)
                          └──────────── one set of services ────────────┘
                 accounts.py   connect / verify / disconnect
                 browse.py     one month, scored, cached
                 reports.py    select, justify, submit, export
                 bundle.py     the ZIP and its contract
                 doctor.py     configuration, with remedies
                          │            │             │
                 connectors/      crypto.py      storage.py
                 graph gmail imap  Key Vault      Blob / disk
                          │            │             │
                   provider APIs   Key Vault    Blob Storage
                                        │
                                  models.py / db  →  PostgreSQL
```

The web app and the CLI are two front ends over one set of services. Neither holds business
rules: `report submit` and the Submit button both call `reports.submit_report`, where
"every item needs a justification" lives. A rule enforced in one front end only is a rule
that does not exist.

## Two authentication planes

| Plane | Mechanism | Answers |
|---|---|---|
| App login | Easy Auth → Entra | May you use ccreport at all? |
| Mailbox connection | Per-account OAuth, or IMAP app password | Which mailboxes may it read for you? |

`auth/principal.py` requires the base64 claims blob as well as the plain name header and
refuses when they disagree, so a bypassed reverse proxy fails loudly instead of silently
authenticating. Roles come from the allow-list table, never from a claim. **An empty
allow-list denies everyone**; the setting seeds the table at boot, the table is
authoritative afterwards, so onboarding is not a redeploy.

## Read-only, structurally

`connectors/base.py` declares `MailConnector` with reads only — no `delete`, `mark_read` or
`move`. A test fails if a mutating-sounding method appears. Underneath: Graph gets
delegated `Mail.Read` (`doctor` **fails** on a write scope), Gmail `gmail.readonly`, IMAP
`EXAMINE` and `BODY.PEEK` so reading does not even mark a message read. The three providers
disagree about folders, search and encodings; every disagreement is resolved inside the
connector so everything above sees one shape.

## What is kept

No message table, no header table. Browsing scores headers and holds them in an in-process
TTL cache (`cache.py`) whose lifetime and size are settings — set the TTL to zero and there
is no retention at all. An integration test watches the PostgreSQL cursor and fails if
browsing emits a write.

Durable: `allowed_principals`, `users`, `mail_accounts`, `oauth_tokens` (ciphertext only),
`reports`, `report_items`, `artifacts`, and an append-only `audit_log`. A selected item
copies sender, subject, date and the vendor/amount hints — never the body, which becomes a
stored PDF or nothing.

## Credentials

Envelope encryption: a fresh 256-bit data key encrypts the secret with AES-GCM, and that
key is wrapped by a KEK. Deployed, the KEK is an RSA key in Key Vault whose private half
never leaves it, so rotation is a re-wrap rather than a forced reconnect for everybody.
Locally it is a symmetric key that refuses to run on Azure or in production — and `Settings`
refuses to construct at all if it is set while App Service is detected. Access tokens live
in memory; only their expiry is persisted.

## Submission

The only moment mail becomes durable, and all-or-nothing:

1. Refuse unless every item has a justification.
2. Re-read each message's attachments. Receipt-like attachments are stored as they are — a
   vendor's own PDF beats our rendering of the mail that carried it. Otherwise render the
   message to PDF.
3. Only then set the status to `submitted`.

On any failure, blobs already written are removed and the transaction rolls back; the report
stays a draft. Rendering is WeasyPrint first, headless Chromium as fallback. Bodies are
attacker-controlled, so HTML is sanitised with `nh3` against an allow-list, remote images
are blocked by default, and `cid:` images are inlined as data URIs. Every rendered PDF
carries a provenance block including the SHA-256 of the source MIME.

## Deployment and testing

Provisioning is `deploy/`, resumable through a ledger at `.ccreport/state.json`; steps
needing an OIT administrator are explicit gates. Schema changes go through Alembic
(`ccreport db upgrade`); `create_all` refuses in a deployed environment.

| Suite | Against | Command |
|---|---|---|
| `tests/unit` | in-process fakes, SQLite | `./ccreport test-local` |
| `tests/render` | real WeasyPrint | `./ccreport test-docker` |
| `tests/integration` | real PostgreSQL and Alembic | `./ccreport test-integration` |

The integration suite runs the migration against a fresh schema and asserts that Alembic
autogenerate finds **no** difference from the ORM metadata, so a model change that skips a
migration fails there.
