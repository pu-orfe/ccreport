# Requests for Princeton OIT

Three external gates stand between a working deployment and a working *production*
deployment. None blocks development — the tests run against in-process fakes — and
`ccreport doctor` reports which are satisfied.

| Gate | Granted by | Symptom if missing |
|---|---|---|
| 1. Entra app, mailbox connector | Entra application admin | Outlook cannot be connected |
| 2. Entra app, Easy Auth | Entra application admin | Nobody can sign in |
| 3. Google consent posture | GCP / Workspace admin | Gmail connections die every 7 days |

The text below is meant to be pasted into a ticket with the placeholders filled in.

---

## 1. Entra app registration — mailbox connector

> Please register an Entra application for `ccreport`, a departmental tool that collects
> monthly expense receipts from faculty mailboxes.
>
> * **Name:** `ccreport-mailbox`
> * **Account types:** any organizational directory and personal Microsoft accounts (`common`)
> * **Redirect URI (Web):** `https://<app-name>.azurewebsites.net/oauth/callback/graph`
> * **Delegated Graph permissions:** `Mail.Read`, `User.Read`, `offline_access`
> * **Application permissions:** none
> * **Client secret:** 24 months, returned via the secure channel
>
> Permissions are **delegated** only: each faculty member authorizes their own mailbox, and
> nothing here can write to, send from, or delete anything. There is no application
> permission and no service principal that can read mail without a user's own consent.
>
> If user consent for `Mail.Read` is disabled in the tenant, please also grant **tenant-wide
> admin consent**. Without it every faculty member sees "Need admin approval".

Record as `CCREPORT_MS_CLIENT_ID` / `CCREPORT_MS_CLIENT_SECRET` (the deploy toolkit stores
them in Key Vault). Verify: `doctor` reports `connector.graph: ok`.

## 2. Entra app registration — Easy Auth

> Please register a second Entra application for the App Service authentication layer of
> `ccreport`.
>
> * **Name:** `ccreport-web`
> * **Account types:** this organizational directory only
> * **Redirect URI (Web):** `https://<app-name>.azurewebsites.net/.auth/login/aad/callback`
> * **ID tokens:** enabled
> * **API permissions:** `User.Read` (delegated) only
>
> This authenticates access to the application itself and grants no mailbox access; that is
> a separate registration and a separate per-user consent.

Kept separate on purpose: conflating them would mean signing in implied mailbox access.
Verify: an unauthenticated request to `/` returns 401 or a redirect, never 200 —
`deploy/steps/90-verify.zsh` refuses to call a deployment verified otherwise.

## 3. Google consent posture

`gmail.readonly` is a **restricted scope**. An External client that has not been through
brand verification and an annual CASA assessment is capped at 100 users, and **every
refresh token expires after seven days** — faculty would reconnect Gmail weekly, which
reads as a broken application.

Either posture below avoids verification and CASA entirely. The first is usually easier.

**Option A — Internal**

> Please move the GCP project `<project-id>` into the `princeton.edu` Cloud Organization so
> its OAuth consent screen can be set to **Internal**. The project hosts the OAuth client
> for `ccreport`, a departmental tool that reads faculty mailboxes to collect expense
> receipts. It requests exactly one scope,
> `https://www.googleapis.com/auth/gmail.readonly`, delegated per user.

**Option B — Trusted**

> Please mark the OAuth client `<client-id>` as **Trusted** for the `princeton.edu`
> Workspace domain (Admin console → Security → API controls → App access control). The
> application is `ccreport`; it requests exactly one scope,
> `https://www.googleapis.com/auth/gmail.readonly`.

Record as `CCREPORT_GOOGLE_CLIENT_ID` / `CCREPORT_GOOGLE_CLIENT_SECRET`, and set
`CCREPORT_GOOGLE_OAUTH_PUBLISHING_STATUS` to `internal` or `trusted`. While it is `testing`
or `unknown`, `doctor` warns and the accounts page tells faculty the connection expires in
seven days.

### Not being requested

* **No domain-wide delegation.** Each faculty member authorizes their own mailbox and can
  revoke it themselves. The alternative is one key able to read every mailbox in the
  university.
* **No service account.** No credential reads mail without a user's consent.
* **No personal Gmail over OAuth.** Personal accounts use IMAP with an app password. The
  OAuth path refuses to switch on until `..._PUBLISHING_STATUS=verified` is declared.

## Housekeeping

* Google deletes OAuth clients idle for 180 days — connect one mailbox over a long break.
* Both Entra client secrets expire; calendar the dates, because the failure mode is every
  mailbox connection breaking at once.
* Rotating a secret does **not** invalidate faculty authorizations. Deleting and re-creating
  the app registration does.
