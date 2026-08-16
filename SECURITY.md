# Security

`ccreport` reads faculty mailboxes. This states what it does with that access and what it
cannot do.

## Reporting a vulnerability

Email the ORFE computing staff or open a private security advisory on the repository. Do not
open a public issue for anything exposing mail content, a credential, or the access list.

## Two authentication planes

| Plane | Mechanism | Answers |
|---|---|---|
| App login | Easy Auth → Entra | May you use ccreport at all? |
| Mailbox connection | Per-account OAuth, or IMAP app password | Which mailboxes may it read for you? |

Signing in never grants mailbox access. Connecting a mailbox is a separate explicit act by
its owner, revocable here and from the provider's own account page.

**Access gate.** Two checks: the principal's domain must match
`CCREPORT_REQUIRED_EMAIL_DOMAIN`, and the principal must appear in the allow-list table. An
**empty allow-list denies everyone**, including a validly authenticated Entra principal —
there is no "empty means open" path anywhere, and tests assert it. Roles come from that
table, never from a token claim.

**Trusting the proxy, carefully.** Easy Auth identifies callers with request headers, which
are only as trustworthy as the guarantee that nothing reaches the origin except through the
proxy. So both `X-MS-CLIENT-PRINCIPAL-NAME` and the base64 claims blob are required, and a
disagreement is refused. That raises the bar from "set one header" to "forge a
self-consistent claims document", and makes the inconsistent case loud.

The development bypass (`CCREPORT_DEV_PRINCIPAL`) is guarded three times: settings refuse to
construct when it is set on App Service, `effective_dev_principal` returns `None` on Azure
and in production, and the UI banners it whenever it is in use. A deployed environment also
refuses to boot without `CCREPORT_SESSION_SECRET`, because unsigned OAuth state and missing
CSRF tokens both fail open.

## Read-only is structural

`MailConnector` declares no method that mutates a mailbox — no `delete`, `mark_read` or
`move` — and a test fails if one appears. Graph gets delegated `Mail.Read` and never
`Mail.ReadWrite` (`ccreport doctor` **fails** on a write scope); Gmail gets
`gmail.readonly`; IMAP issues `EXAMINE` and `BODY.PEEK`, so reading does not even mark a
message as read.

There is **no domain-wide delegation and no service account**. No credential can read a
mailbox without that mailbox owner's consent.

## Retention

**Nothing from a mailbox reaches durable storage unless a faculty member selected it.**

* No `messages` table, no `headers` table.
* Browse results live in memory for `CCREPORT_HEADER_CACHE_TTL_SECONDS` (default 600) and
  are lost on restart; `0` disables caching entirely.
* Entries are keyed per user and dropped immediately when an account is disconnected.
* An integration test watches the PostgreSQL cursor and fails if browsing writes anything.

A selected item stores sender, subject, date and vendor/amount hints — never the body, which
becomes a stored PDF or nothing. The audit log has no `updated_at`: nothing rewrites a row.

## Credentials at rest

Refresh tokens and IMAP app passwords use envelope encryption: a fresh 256-bit data key
encrypts the secret with AES-GCM (random 96-bit nonce, never reused), and that key is
wrapped by a KEK. Deployed, the KEK is an RSA key in Key Vault whose private half never
leaves the vault; the key version is recorded per record, so rotation is a background
re-wrap rather than a forced reconnect. Access tokens are held in memory only; the database
records their expiry, never their value.

## Hostile input

A message body is chosen by anyone who can email a faculty member.

* HTML is sanitised with `nh3` against a tag/attribute allow-list before any renderer sees
  it. Scripts, event handlers, styles, frames and objects do not survive.
* Remote images are blocked by default — loading them confirms delivery to the sender.
  Inline `cid:` images are embedded from the message's own parts.
* URL schemes are limited to `http`, `https`, `mailto`, `data`.
* Attachment filenames are reduced to a safe basename and never used as a storage path;
  paths are generated from database identifiers and traversal is rejected by the store.
* Attachments over `CCREPORT_MAX_ATTACHMENT_MB` (default 25) are skipped and the message is
  rendered instead.

## Web application

Server-rendered forms; no client framework, no browser-facing JSON API. Every
state-changing POST carries a signed, expiring form token bound to the acting principal,
salted separately from OAuth state so neither can be replayed as the other. OAuth uses PKCE
(S256) with signed `state` valid for ten minutes. Error redirects use only a same-origin
path, so no page can be turned into an open redirect. `/docs` and `/openapi.json` are
disabled. `/healthz` is unauthenticated and says nothing about configuration;
`/api/connectors/posture` requires authentication.

## Administrators

An administrator can list **submitted** reports, download their bundles, and manage the
access list. Drafts are never listed — an unsubmitted report is a faculty member's working
state, and reading it would make this a surveillance tool rather than a handoff.
Administrators cannot browse anybody's mailbox and cannot remove their own access. Every
grant, revocation, connection, submission and export is written to the audit log.

## Known limits

* Bundle downloads are served through the application, so an administrator's reach is only
  as good as the allow-list and the Easy Auth configuration.
* Amount and vendor extraction is a hint from message text, not an authority;
  `ccworks-apply.json` assumes the consumer re-checks against the live expense report.
* A `testing`-posture Google client expires refresh tokens after seven days. ccreport warns
  rather than pretending otherwise; see [`docs/OIT-REQUESTS.md`](docs/OIT-REQUESTS.md).
