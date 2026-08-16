# ccreport

Monthly receipt collection for faculty, bundled for administrators.

A sister application to [`ccworks`](https://github.com/pu-orfe/ccworks). Where `ccworks`
drives SAP Concur, `ccreport` fixes the step before it: getting complete, consistent
receipts and justifications out of faculty mailboxes and into an administrator's hands
without a month of reminder emails.

Faculty connect one or more **read-only** mailboxes, browse a month, mark which messages
are receipts, say why the money was spent, and submit. Administrators get a console of
submitted reports and a fixed-format ZIP bundle per person per month.

---

## Setup

```bash
git clone git@github.com:pu-orfe/ccreport.git
cd ccreport
./ccreport setup                      # .venv + editable install
cp .env.example .env
./ccreport doctor --generate-key      # paste into CCREPORT_DEV_ENCRYPTION_KEY
```

Then set four values in `.env` and start:

```ini
CCREPORT_DEV_PRINCIPAL=you@princeton.edu      # stands in for Easy Auth; refused on Azure
CCREPORT_ALLOWED_PRINCIPALS=you@princeton.edu # empty means DENY ALL
CCREPORT_ADMIN_PRINCIPALS=you@princeton.edu
CCREPORT_DATABASE_URL=sqlite:///.ccreport/dev.db
```

```bash
./ccreport db upgrade
./ccreport doctor                     # says what is still missing, and what to do
./ccreport serve                      # http://127.0.0.1:8000
```

`ccreport doctor` is the answer to "is this configured?" at every stage. Connecting real
mailboxes additionally needs the provider registrations in
[`docs/OIT-REQUESTS.md`](docs/OIT-REQUESTS.md); nothing else here depends on them, because
the test suite runs against in-process fakes.

## Deploy, update, teardown

Azure App Service (Linux container), Blob Storage, PostgreSQL Flexible Server and Key
Vault, provisioned by a resumable toolkit.

```bash
deploy/ccreport-azure doctor    # tooling, sign-in, manual gates; changes nothing
./ccreport deploy               # interactive; resumes if interrupted
./ccreport deploy --dry-run     # prints every az command, runs none
./ccreport update               # rebuild the image and restart; no provisioning
./ccreport teardown             # delete what the ledger created
./ccreport status               # the ledger, beside what Azure actually has
```

Progress is recorded in `.ccreport/state.json`, so an interrupted deploy resumes rather
than restarts, and the same scripts run non-interactively in CI with `--yes
--no-reprompt`. Steps needing a Princeton OIT administrator are explicit gates that print
the exact request to send.

Schema changes are Alembic migrations applied in the container with `ccreport db upgrade`.
`db create-all` refuses to run outside development.

## Tests

```bash
./ccreport test-local           # unit + rendering, on the host
./ccreport test-docker          # the same, in the container, with a working WeasyPrint
./ccreport test-integration     # against real PostgreSQL in Compose
```

No test needs a credential or a network.

## Commands

`./ccreport <args>` and an installed `ccreport <args>` mean the same thing. The launcher
adds only checkout chores: `setup`, `test-local`, `test-docker`, `test-integration`,
`serve`, and the Azure verbs `deploy` / `update` / `teardown` / `status` / `logs` / `open`.

| Group | Command | Scope |
| :--- | :--- | :--- |
| **account** | `list`, `connect PROVIDER`, `test [ID]`, `disconnect ID` | Connected mailboxes. |
| **mailbox** | `list --account ID` | Folders or labels. |
| **messages** | `list --account ID --month YYYY-MM` | A month of headers, scored. Filters: `--mailbox`, `--receipts-only`, `--from`, `--subject`. |
| **report** | `list`, `show PERIOD`, `create --month YYYY-MM` | Your reports. |
| | `add PERIOD --account ID --message ID` | Select a message. |
| | `justify PERIOD --item N --text "…"`, `remove PERIOD --item N` | Edit a draft. |
| | `submit PERIOD`, `export PERIOD [--out PATH]` | Freeze it; write the ZIP. |
| **admin** | `reports`, `download PERIOD --user UPN`, `allow add\|remove\|list` | Submitted reports and the access list. |
| **db** | `upgrade [REV]`, `current`, `create-all` | Migrations. |
| — | `serve`, `doctor` | Run the web UI; check configuration. |

Global flags work anywhere in the argument list: `-V/--version`, `-v/--verbose`,
`--output {json,text}`, `-P/--principal UPN`.

**stdout is data, stderr is diagnostics**, so `2>/dev/null | jq` always works. The CLI has
no Easy Auth in front of it, so it needs `--principal`, `CCREPORT_PRINCIPAL`, or
`CCREPORT_DEV_PRINCIPAL` — still checked against the allow-list.

## The bundle

`ccreport-<user>-<YYYY-MM>.zip`:

```
manifest.json        schema-versioned; every item, artifact, hash and justification
summary.csv          date, vendor, amount, justification, filename
index.html           printable contact sheet
ccworks-apply.json   ready for `ccworks report apply-json`
receipts/001-….pdf   stable, zero-padded, collision-safe filenames
```

`ccworks-apply.json` deliberately omits `index`: Concur row indices are positional and must
be re-read at apply time. Vendor and amount are carried instead, which is what lets
`apply-json` refuse to write a receipt to the wrong expense. Full contract in
[`docs/CCWORKS-HANDOFF.md`](docs/CCWORKS-HANDOFF.md).

## How it is built

- **Two authentication planes.** Easy Auth answers "may you use ccreport at all"; a
  separate per-mailbox OAuth or IMAP app password answers "which mailboxes may it read for
  you". Signing in never grants mailbox access, and an **empty allow-list denies everyone**.
- **Read-only is structural.** `MailConnector` declares no method that mutates a mailbox.
  Graph gets `Mail.Read`, Gmail gets `gmail.readonly`, IMAP issues `EXAMINE` and
  `BODY.PEEK`.
- **Nothing from a mailbox is stored unless it was selected.** There is no message table
  and no header table; browsing lives in an expiring in-process cache, and a test asserts
  browsing performs no durable write.

| Document | Contents |
|---|---|
| [`docs/OIT-REQUESTS.md`](docs/OIT-REQUESTS.md) | Copy-paste requests for the Entra and Google gates |
| [`docs/CCWORKS-HANDOFF.md`](docs/CCWORKS-HANDOFF.md) | The bundle and `apply-json` contract |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | How the pieces fit, and why |
| [`SECURITY.md`](SECURITY.md) | Auth planes, retention, read-only enforcement |

## Licence

MIT. See [`LICENSE.md`](LICENSE.md).
