"""The ccreport command line.

Two conventions, both borrowed from ``ccworks`` and both worth stating:

**Commands are ``<group> <subcommand>``.** ``account list``, ``report submit``,
``admin allow add``. A flat namespace of forty verbs is unmemorable; six groups
of six are not.

**stdout is data, stderr is diagnostics.** Query commands print JSON on stdout
while logs, prompts and progress go to stderr, so ``ccreport report show 2026-07
2>/dev/null | jq`` always works. ``--output text`` swaps the data format and
nothing else.

Global flags are accepted anywhere in the argument list, because
``ccreport report show 2026-07 --output text`` is what people actually type.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from . import __version__
from .errors import CCReportError, ConfigError, NotAuthorized
from .models import User
from .settings import Settings, get_settings

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_DENIED = 3
EXIT_CONFIG = 4

logger = logging.getLogger("ccreport.cli")


# ------------------------------------------------------------------- plumbing
@dataclass(slots=True)
class Context:
    settings: Settings
    session: Session
    user: User
    output: str
    verbose: int


def _configure_logging(verbose: int, settings: Settings) -> None:
    level = logging.DEBUG if verbose > 1 else logging.INFO if verbose else settings.log_level.upper()
    logging.basicConfig(
        level=level,
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )


#: Keys whose values never belong on a terminal, in a pipe, or in a shell's
#: history file. Matched on the key, so it holds however a caller builds a dict.
_SECRETISH = re.compile(r"password|secret|credential|passphrase|private", re.IGNORECASE)
_SECRET_KEYS = frozenset(
    {"token", "access_token", "refresh_token", "api_key", "authorization", "cookie"}
)
REDACTED = "***"


def redact(value: Any) -> Any:
    """Replace anything that looks like a credential with ``***``.

    Applied to everything printed, rather than trusting each command to hand
    over a clean dict. One command that forgets is one credential in somebody's
    scrollback, and the commands that handle credentials — ``account connect``
    above all — are exactly the ones whose output people paste into tickets.

    Key names are matched, never values, so ``authorization_url`` survives while
    ``app_password`` does not.
    """
    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if _SECRETISH.search(str(key)) or str(key).lower() in _SECRET_KEYS
                else redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


def emit(data: Any, output: str = "json") -> None:
    """Print a result. JSON on stdout, or a terse human rendering."""
    safe = redact(data)
    if output == "json":
        print(json.dumps(safe, indent=2, default=str))
        return
    print(_as_text(safe))


def _as_text(data: Any, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(data, list):
        if not data:
            return f"{pad}(none)"
        if all(isinstance(row, dict) for row in data):
            return "\n\n".join(_as_text(row, indent) for row in data)
        return "\n".join(f"{pad}{item}" for item in data)
    if isinstance(data, dict):
        lines = []
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.append(_as_text(value, indent + 1))
            else:
                lines.append(f"{pad}{key}: {'' if value is None else value}")
        return "\n".join(lines)
    return f"{pad}{data}"


def _acting_principal(args: argparse.Namespace, settings: Settings) -> str:
    """Whose account the CLI is acting on.

    The CLI has no Easy Auth in front of it, so identity is explicit: a flag, an
    environment variable, or the development principal. It is still checked
    against the allow-list — the CLI is not a way around the access gate.
    """
    upn = getattr(args, "principal", None) or os.environ.get("CCREPORT_PRINCIPAL")
    upn = (upn or settings.effective_dev_principal or "").strip().lower()
    if not upn:
        raise ConfigError(
            "no principal. Pass --principal UPN, set CCREPORT_PRINCIPAL, or set "
            "CCREPORT_DEV_PRINCIPAL for local development."
        )
    return upn


def _authorize(session: Session, upn: str, settings: Settings, *, require_role: str | None = None) -> User:
    from .auth import Principal, authorize, bootstrap_allow_list

    bootstrap_allow_list(session, settings)
    session.flush()
    return authorize(session, Principal(upn=upn), settings, require_role=require_role)


# -------------------------------------------------------------------- account
def cmd_account_list(ctx: Context, args: argparse.Namespace) -> int:
    from .accounts import account_summary, list_accounts

    accounts = list_accounts(ctx.session, ctx.user, include_revoked=args.all)
    emit([account_summary(a) for a in accounts], ctx.output)
    return EXIT_OK


def cmd_account_connect(ctx: Context, args: argparse.Namespace) -> int:
    from .accounts import account_summary, connect_imap_account, connect_oauth_account
    from .oauth import (
        StateSigner,
        build_oauth_client,
        code_challenge_for,
        new_code_verifier,
        redirect_uri_for,
    )

    provider = args.provider
    if provider == "imap":
        password = args.app_password or os.environ.get("CCREPORT_IMAP_APP_PASSWORD")
        if not password and sys.stdin.isatty():
            password = getpass.getpass("IMAP app password (input hidden): ")
        if not args.address or not password:
            raise ConfigError(
                "connecting IMAP needs --address and an app password "
                "(--app-password, CCREPORT_IMAP_APP_PASSWORD, or an interactive prompt)"
            )
        account = connect_imap_account(
            ctx.session,
            ctx.user,
            args.address,
            password,
            host=args.host,
            port=args.port,
            settings=ctx.settings,
        )
        emit(account_summary(account), ctx.output)
        return EXIT_OK

    client = build_oauth_client(provider, ctx.settings)
    signer = StateSigner.from_settings(ctx.settings)
    redirect_uri = redirect_uri_for(provider, ctx.settings)

    if not args.code:
        verifier = new_code_verifier()
        state = signer.sign({"upn": ctx.user.upn, "provider": provider, "verifier": verifier})
        url = client.authorization_url(
            redirect_uri=redirect_uri, state=state, code_challenge=code_challenge_for(verifier)
        )
        print(
            "Open this URL, approve read-only access, then run the same command "
            "again with --code and --state from the redirect.",
            file=sys.stderr,
        )
        emit({"provider": provider, "authorization_url": url, "state": state, "redirect_uri": redirect_uri}, ctx.output)
        return EXIT_OK

    if not args.state:
        raise ConfigError("--code must be accompanied by the --state value printed earlier")
    payload = signer.unsign(args.state)
    if payload.get("upn") != ctx.user.upn or payload.get("provider") != provider:
        raise NotAuthorized("this authorization state was issued for a different user or provider")

    tokens = client.exchange_code(
        args.code, redirect_uri=redirect_uri, code_verifier=payload["verifier"]
    )
    account = connect_oauth_account(
        ctx.session, ctx.user, provider, tokens, settings=ctx.settings, address=args.address
    )
    emit(account_summary(account), ctx.output)
    return EXIT_OK


def cmd_account_test(ctx: Context, args: argparse.Namespace) -> int:
    from .accounts import list_accounts, resolve_account, verify_account

    accounts = (
        [resolve_account(ctx.session, ctx.user, args.account)]
        if args.account
        else list_accounts(ctx.session, ctx.user)
    )
    results = []
    for account in accounts:
        status = verify_account(ctx.session, ctx.user, account, settings=ctx.settings)
        results.append(
            {
                "id": account.id,
                "provider": status.provider,
                "address": status.address,
                "ok": status.ok,
                "detail": status.detail,
                "needs_reauth": status.needs_reauth,
                "warnings": list(status.warnings),
            }
        )
    emit(results if len(results) != 1 else results[0], ctx.output)
    return EXIT_OK if all(r["ok"] for r in results) else EXIT_ERROR


def cmd_account_disconnect(ctx: Context, args: argparse.Namespace) -> int:
    from .accounts import disconnect_account, resolve_account

    account = resolve_account(ctx.session, ctx.user, args.account)
    address, provider, account_id = account.address, account.provider, account.id
    disconnect_account(ctx.session, ctx.user, account.id)
    emit(
        {"disconnected": account_id, "address": address, "provider": provider, "credential": "deleted"},
        ctx.output,
    )
    return EXIT_OK


# -------------------------------------------------------------------- mailbox
def cmd_mailbox_list(ctx: Context, args: argparse.Namespace) -> int:
    from .accounts import open_connector, resolve_account
    from .browse import list_folders

    account = resolve_account(ctx.session, ctx.user, args.account)
    connector = open_connector(ctx.session, account, settings=ctx.settings)
    emit(list_folders(connector), ctx.output)
    return EXIT_OK


# ------------------------------------------------------------------- messages
def cmd_messages_list(ctx: Context, args: argparse.Namespace) -> int:
    from .accounts import open_connector, resolve_account
    from .browse import browse

    account = resolve_account(ctx.session, ctx.user, args.account)
    connector = open_connector(ctx.session, account, settings=ctx.settings)
    result = browse(
        ctx.session,
        ctx.user,
        account,
        args.month,
        connector=connector,
        folder_ids=tuple(args.mailbox or ()),
        subject_contains=args.subject,
        from_contains=getattr(args, "from"),
        receipts_only=args.receipts_only,
        has_attachments_only=args.with_attachments,
        limit=args.limit,
        settings=ctx.settings,
    )
    emit(result.as_dict(), ctx.output)
    return EXIT_OK


# --------------------------------------------------------------------- report
def _open_connector_factory(ctx: Context):
    from .accounts import open_connector

    def factory(account):
        return open_connector(ctx.session, account, settings=ctx.settings)

    return factory


def cmd_report_list(ctx: Context, args: argparse.Namespace) -> int:
    from .reports import list_reports, report_summary

    reports = list_reports(ctx.session, ctx.user)
    emit([report_summary(r, include_items=False) for r in reports], ctx.output)
    return EXIT_OK


def cmd_report_show(ctx: Context, args: argparse.Namespace) -> int:
    from .reports import get_report, report_summary

    report = get_report(ctx.session, ctx.user, args.period)
    emit(report_summary(report), ctx.output)
    return EXIT_OK


def cmd_report_create(ctx: Context, args: argparse.Namespace) -> int:
    from .reports import create_report, report_summary

    report = create_report(ctx.session, ctx.user, args.month, title=args.title, settings=ctx.settings)
    emit(report_summary(report), ctx.output)
    return EXIT_OK


def cmd_report_add(ctx: Context, args: argparse.Namespace) -> int:
    from .accounts import open_connector, resolve_account
    from .browse import find_header
    from .reports import add_item, create_report, item_summary

    account = resolve_account(ctx.session, ctx.user, args.account)
    report = create_report(ctx.session, ctx.user, args.period, settings=ctx.settings)
    connector = open_connector(ctx.session, account, settings=ctx.settings)
    header = find_header(
        ctx.session, ctx.user, account, args.period, args.message,
        connector=connector, settings=ctx.settings,
    )
    if header is None:
        raise CCReportError(
            f"message {args.message!r} is not in {args.period} for {account.address}. "
            "List the month first with `messages list`."
        )
    item = add_item(ctx.session, ctx.user, report, account, header)
    if args.justification:
        item.justification = args.justification.strip()
    emit(item_summary(item), ctx.output)
    return EXIT_OK


def cmd_report_justify(ctx: Context, args: argparse.Namespace) -> int:
    from .reports import get_report, item_summary, justify_item

    report = get_report(ctx.session, ctx.user, args.period)
    item = justify_item(ctx.session, ctx.user, report, args.item, args.text)
    emit(item_summary(item), ctx.output)
    return EXIT_OK


def cmd_report_remove(ctx: Context, args: argparse.Namespace) -> int:
    from .reports import get_report, remove_item, report_summary

    report = get_report(ctx.session, ctx.user, args.period)
    remove_item(ctx.session, ctx.user, report, args.item)
    emit(report_summary(report), ctx.output)
    return EXIT_OK


def cmd_report_submit(ctx: Context, args: argparse.Namespace) -> int:
    from .reports import get_report, report_summary, submit_report

    report = get_report(ctx.session, ctx.user, args.period)
    submit_report(
        ctx.session,
        ctx.user,
        report,
        open_connector=_open_connector_factory(ctx),
        settings=ctx.settings,
    )
    emit(report_summary(report), ctx.output)
    return EXIT_OK


def _write_bundle(filename: str, content: bytes, out: str | None) -> Path:
    from .paths import bundle_dir

    target = Path(out).expanduser() if out else bundle_dir() / filename
    if target.is_dir():
        target = target / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def cmd_report_export(ctx: Context, args: argparse.Namespace) -> int:
    from .reports import export_bundle, get_report

    report = get_report(ctx.session, ctx.user, args.period)
    filename, content = export_bundle(ctx.session, report, ctx.user, settings=ctx.settings)
    target = _write_bundle(filename, content, args.out)
    emit({"period": report.period, "path": str(target), "bytes": len(content)}, ctx.output)
    return EXIT_OK


# ---------------------------------------------------------------------- admin
def cmd_admin_reports(ctx: Context, args: argparse.Namespace) -> int:
    from .reports import admin_list_reports, report_summary

    rows = admin_list_reports(
        ctx.session, ctx.user, upn=args.user, period=args.month, include_drafts=args.include_drafts
    )
    emit(
        [
            {"user": owner.upn, "display_name": owner.display_name, **report_summary(report, include_items=False)}
            for owner, report in rows
        ],
        ctx.output,
    )
    return EXIT_OK


def cmd_admin_download(ctx: Context, args: argparse.Namespace) -> int:
    from .reports import admin_list_reports, export_bundle

    rows = admin_list_reports(ctx.session, ctx.user, upn=args.user, period=args.period, include_drafts=False)
    if not rows:
        raise CCReportError(f"no submitted report for {args.user} in {args.period}")
    owner, report = rows[0]
    filename, content = export_bundle(
        ctx.session, report, owner, settings=ctx.settings, actor=ctx.user
    )
    target = _write_bundle(filename, content, args.out)
    emit(
        {"user": owner.upn, "period": report.period, "path": str(target), "bytes": len(content)},
        ctx.output,
    )
    return EXIT_OK


def cmd_admin_allow(ctx: Context, args: argparse.Namespace) -> int:
    from .auth import ROLE_ADMIN, ROLE_FACULTY, grant_access, list_access, revoke_access

    if args.allow_action == "list":
        emit(
            [
                {
                    "upn": row.upn,
                    "role": row.role,
                    "seeded": row.seeded,
                    "added_by": row.added_by,
                    "note": row.note,
                }
                for row in list_access(ctx.session)
            ],
            ctx.output,
        )
        return EXIT_OK

    if args.allow_action == "add":
        role = ROLE_ADMIN if args.role == "admin" else ROLE_FACULTY
        record = grant_access(
            ctx.session, args.upn, role=role, added_by=ctx.user.upn, note=args.note
        )
        emit({"upn": record.upn, "role": record.role, "granted": True}, ctx.output)
        return EXIT_OK

    removed = revoke_access(ctx.session, args.upn, removed_by=ctx.user.upn)
    emit({"upn": args.upn.strip().lower(), "removed": removed}, ctx.output)
    return EXIT_OK if removed else EXIT_ERROR


# ------------------------------------------------------------------------- db
def _alembic_config(settings: Settings):
    from alembic.config import Config

    package_root = Path(__file__).resolve().parent
    config = Config()
    config.set_main_option("script_location", str(package_root / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    return config


def cmd_db_upgrade(ctx: Context | None, args: argparse.Namespace, settings: Settings) -> int:
    from alembic import command

    command.upgrade(_alembic_config(settings), args.revision)
    emit({"upgraded_to": args.revision, "database": _redact_url(settings.database_url)}, args.output)
    return EXIT_OK


def cmd_db_current(ctx: Context | None, args: argparse.Namespace, settings: Settings) -> int:
    from alembic import command

    command.current(_alembic_config(settings), verbose=bool(args.verbose))
    return EXIT_OK


def cmd_db_create_all(ctx: Context | None, args: argparse.Namespace, settings: Settings) -> int:
    from .db import create_all

    if settings.on_azure or settings.is_production:
        raise ConfigError(
            "refusing to create tables directly in a deployed environment; run "
            "`ccreport db upgrade` so the schema change is a reviewed migration."
        )
    create_all(settings)
    emit({"created": True, "database": _redact_url(settings.database_url)}, args.output)
    return EXIT_OK


def _redact_url(url: str) -> str:
    if "://" not in url or "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    credentials, _, host = rest.rpartition("@")
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


# --------------------------------------------------------------------- global
def cmd_doctor(ctx: Context | None, args: argparse.Namespace, settings: Settings) -> int:
    from .crypto import generate_dev_key
    from .doctor import diagnose

    if args.generate_key:
        emit({"CCREPORT_DEV_ENCRYPTION_KEY": generate_dev_key()}, args.output)
        return EXIT_OK

    allow_count: int | None = None
    if args.check_database:
        try:
            from .auth import list_access
            from .db import session_scope

            with session_scope(settings) as session:
                allow_count = len(list_access(session))
        except Exception as exc:  # the database check itself reports the failure
            logger.debug("allow-list count unavailable: %s", exc)

    diagnosis = diagnose(settings, allow_list_count=allow_count, check_database=args.check_database)
    emit(diagnosis.as_dict(), args.output)
    return EXIT_OK if diagnosis.ok else EXIT_ERROR


def cmd_serve(ctx: Context | None, args: argparse.Namespace, settings: Settings) -> int:
    try:
        import uvicorn
    except ImportError as exc:
        raise ConfigError("serving needs the web extra; install ccreport[web]") from exc

    uvicorn.run(
        "ccreport.web.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=(settings.log_level or "info").lower(),
    )
    return EXIT_OK


#: Commands that do not need a database session or an authorized principal.
_STANDALONE = {
    "doctor": cmd_doctor,
    "serve": cmd_serve,
    "db.upgrade": cmd_db_upgrade,
    "db.current": cmd_db_current,
    "db.create-all": cmd_db_create_all,
}


# -------------------------------------------------------------------- parsing
def _global_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-V", "--version", action="store_true", help="print the version and exit")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="more logging on stderr")
    parser.add_argument(
        "--output", choices=("json", "text"), default="json", help="stdout format (default: json)"
    )
    parser.add_argument(
        "-P", "--principal", default=None, help="act as this UPN (CLI has no Easy Auth in front of it)"
    )
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ccreport",
        description="Monthly receipt collection for faculty, bundled for administrators.",
        parents=[_global_parser()],
    )
    groups = parser.add_subparsers(dest="group", metavar="<group>")

    # -- account
    account = groups.add_parser("account", help="connected mailboxes").add_subparsers(
        dest="command", metavar="<command>", required=True
    )
    p = account.add_parser("list", help="connected mailboxes and their status")
    p.add_argument("--all", action="store_true", help="include revoked connections")
    p.set_defaults(func=cmd_account_list)

    p = account.add_parser("connect", help="start (or finish) a mailbox authorization")
    p.add_argument("provider", choices=("graph", "gmail", "imap"))
    p.add_argument("--address", help="mailbox address; required for IMAP")
    p.add_argument("--app-password", help="IMAP app password (prefer the prompt or the environment)")
    p.add_argument("--host", help="IMAP host (default from settings)")
    p.add_argument("--port", type=int, help="IMAP port (default from settings)")
    p.add_argument("--code", help="authorization code from the OAuth redirect")
    p.add_argument("--state", help="the state value printed when the authorization started")
    p.set_defaults(func=cmd_account_connect)

    p = account.add_parser("test", help="verify a credential and report posture warnings")
    p.add_argument("account", nargs="?", help="id, address or provider; all accounts when omitted")
    p.set_defaults(func=cmd_account_test)

    p = account.add_parser("disconnect", help="revoke and forget a connection")
    p.add_argument("account", help="id, address or provider")
    p.set_defaults(func=cmd_account_disconnect)

    # -- mailbox
    mailbox = groups.add_parser("mailbox", help="folders and labels").add_subparsers(
        dest="command", metavar="<command>", required=True
    )
    p = mailbox.add_parser("list", help="folders or labels available to select")
    p.add_argument("--account", required=True)
    p.set_defaults(func=cmd_mailbox_list)

    # -- messages
    messages = groups.add_parser("messages", help="browse a month of mail").add_subparsers(
        dest="command", metavar="<command>", required=True
    )
    p = messages.add_parser("list", help="a month of headers, scored")
    p.add_argument("--account", required=True)
    p.add_argument("--month", required=True, metavar="YYYY-MM")
    p.add_argument("--mailbox", action="append", help="restrict to a folder or label (repeatable)")
    p.add_argument("--receipts-only", action="store_true", help="show only likely receipts")
    p.add_argument("--with-attachments", action="store_true", help="ask the provider for attachments only")
    p.add_argument("--from", dest="from", help="substring match on the sender")
    p.add_argument("--subject", help="substring match on the subject")
    p.add_argument("--limit", type=int, default=500)
    p.set_defaults(func=cmd_messages_list)

    # -- report
    report = groups.add_parser("report", help="your monthly reports").add_subparsers(
        dest="command", metavar="<command>", required=True
    )
    p = report.add_parser("list", help="reports for the signed-in user")
    p.set_defaults(func=cmd_report_list)

    p = report.add_parser("show", help="one report and its items")
    p.add_argument("period", metavar="YYYY-MM")
    p.set_defaults(func=cmd_report_show)

    p = report.add_parser("create", help="start a draft")
    p.add_argument("--month", required=True, metavar="YYYY-MM")
    p.add_argument("--title")
    p.set_defaults(func=cmd_report_create)

    p = report.add_parser("add", help="select a message")
    p.add_argument("period", metavar="YYYY-MM")
    p.add_argument("--account", required=True)
    p.add_argument("--message", required=True, help="provider message id from `messages list`")
    p.add_argument("--justification", help="record the justification at the same time")
    p.set_defaults(func=cmd_report_add)

    p = report.add_parser("justify", help="attach a justification")
    p.add_argument("period", metavar="YYYY-MM")
    p.add_argument("--item", required=True, help="1-based position, or an item id")
    p.add_argument("--text", required=True)
    p.set_defaults(func=cmd_report_justify)

    p = report.add_parser("remove", help="deselect a message")
    p.add_argument("period", metavar="YYYY-MM")
    p.add_argument("--item", required=True)
    p.set_defaults(func=cmd_report_remove)

    p = report.add_parser("submit", help="freeze the report and build its artifacts")
    p.add_argument("period", metavar="YYYY-MM")
    p.set_defaults(func=cmd_report_submit)

    p = report.add_parser("export", help="write the ZIP bundle")
    p.add_argument("period", metavar="YYYY-MM")
    p.add_argument("--out", help="file or directory (default: the local bundle directory)")
    p.set_defaults(func=cmd_report_export)

    # -- admin
    admin = groups.add_parser("admin", help="administrator console").add_subparsers(
        dest="command", metavar="<command>", required=True
    )
    p = admin.add_parser("reports", help="every submitted report")
    p.add_argument("--user", metavar="UPN")
    p.add_argument("--month", metavar="YYYY-MM")
    p.add_argument("--include-drafts", action="store_true", help="also list unsubmitted drafts")
    p.set_defaults(func=cmd_admin_reports, require_role="admin")

    p = admin.add_parser("download", help="fetch somebody's bundle")
    p.add_argument("period", metavar="YYYY-MM")
    p.add_argument("--user", required=True, metavar="UPN")
    p.add_argument("--out")
    p.set_defaults(func=cmd_admin_download, require_role="admin")

    p = admin.add_parser("allow", help="manage the allow-list")
    p.add_argument("allow_action", choices=("add", "remove", "list"))
    p.add_argument("upn", nargs="?")
    p.add_argument("--role", choices=("faculty", "admin"), default="faculty")
    p.add_argument("--note")
    p.set_defaults(func=cmd_admin_allow, require_role="admin")

    # -- db
    database = groups.add_parser("db", help="schema migrations").add_subparsers(
        dest="command", metavar="<command>", required=True
    )
    p = database.add_parser("upgrade", help="run Alembic migrations")
    p.add_argument("revision", nargs="?", default="head")
    p.set_defaults(func=cmd_db_upgrade, standalone="db.upgrade")

    p = database.add_parser("current", help="show the applied revision")
    p.set_defaults(func=cmd_db_current, standalone="db.current")

    p = database.add_parser("create-all", help="create tables directly (development only)")
    p.set_defaults(func=cmd_db_create_all, standalone="db.create-all")

    # -- standalone
    p = groups.add_parser("doctor", help="check configuration and gates")
    p.add_argument("--check-database", action="store_true", help="also connect to the database")
    p.add_argument("--generate-key", action="store_true", help="print a development encryption key")
    p.set_defaults(func=cmd_doctor, standalone="doctor")

    p = groups.add_parser("serve", help="run the web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve, standalone="serve")

    return parser


def _split_globals(argv: Sequence[str]) -> tuple[argparse.Namespace, list[str]]:
    """Pull global flags out of anywhere in the argument list."""
    known, rest = _global_parser().parse_known_args(list(argv))
    return known, rest


# ----------------------------------------------------------------------- main
def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    globals_ns, remainder = _split_globals(argv)

    if globals_ns.version:
        print(__version__)
        return EXIT_OK

    parser = build_parser()
    if not remainder:
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    args = parser.parse_args(remainder)
    for name in ("output", "verbose", "principal"):
        setattr(args, name, getattr(globals_ns, name))
    if not getattr(args, "func", None):
        parser.print_help(sys.stderr)
        return EXIT_USAGE

    try:
        settings = get_settings()
        _configure_logging(args.verbose, settings)

        standalone = getattr(args, "standalone", None)
        if standalone in _STANDALONE:
            return _STANDALONE[standalone](None, args, settings)

        from .db import session_scope

        with session_scope(settings) as session:
            upn = _acting_principal(args, settings)
            user = _authorize(session, upn, settings, require_role=getattr(args, "require_role", None))
            ctx = Context(
                settings=settings,
                session=session,
                user=user,
                output=args.output,
                verbose=args.verbose,
            )
            return args.func(ctx, args)
    except NotAuthorized as exc:
        print(f"ccreport: {exc}", file=sys.stderr)
        return EXIT_DENIED
    except ConfigError as exc:
        print(f"ccreport: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except CCReportError as exc:
        print(f"ccreport: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except SQLAlchemyError as exc:
        # An unreachable database is the most common local failure by far, and a
        # SQLAlchemy traceback is a poor way to learn that Postgres is not running.
        print(
            f"ccreport: the database could not be reached ({exc.__class__.__name__}). "
            "Check CCREPORT_DATABASE_URL, and run `ccreport db upgrade` if the "
            "schema has never been created.",
            file=sys.stderr,
        )
        logger.debug("database error", exc_info=exc)
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("interrupted", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
