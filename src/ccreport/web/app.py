"""The web application: the surface faculty actually touch.

Server-rendered forms, no client framework, no JSON API for the browser to
consume. That is a deliberate choice for an application whose entire job is a
list, some checkboxes and a text box per row: it works with the keyboard, it
works with a screen reader, and it has no build step to rot between semesters.

The authentication plane is Easy Auth in front of the container. This process
never sees a password and never issues a session cookie; identity arrives as
request headers, is parsed with suspicion in :mod:`ccreport.auth.principal`, and
is checked against the allow-list on every request. There is no login route
here, because there is nothing here to log in to.

The mailbox plane is entirely separate: ``/accounts/connect/{provider}`` starts
an OAuth authorization the *user* completes with their own provider, and
``/oauth/callback/{provider}`` finishes it. Signing in never grants mailbox
access.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .. import __version__
from ..accounts import (
    account_summary,
    connect_imap_account,
    connect_oauth_account,
    disconnect_account,
    get_account,
    list_accounts,
    open_connector,
    verify_account,
)
from ..auth import (
    ROLE_ADMIN,
    ROLE_FACULTY,
    InconsistentPrincipal,
    Principal,
    PrincipalError,
    authorize,
    bootstrap_allow_list,
    grant_access,
    list_access,
    resolve_principal,
    revoke_access,
)
from ..browse import browse, find_header, list_folders
from ..db import get_sessionmaker
from ..doctor import connector_posture
from ..errors import CCReportError, ConfigError, NotAuthorized, ReauthRequired
from ..models import MailAccount, User
from ..oauth import (
    StateSigner,
    build_oauth_client,
    code_challenge_for,
    new_code_verifier,
    redirect_uri_for,
)
from ..period import available_periods, clamp_period, format_period, parse_period, period_label
from ..reports import (
    add_item,
    admin_list_reports,
    create_report,
    empty_report_summary,
    export_bundle,
    find_report,
    get_report,
    justify_item,
    list_reports,
    remove_item,
    report_summary,
    submit_report,
)
from ..settings import Settings, get_settings

logger = logging.getLogger("ccreport.web")

_HERE = Path(__file__).resolve().parent
_CSRF_SALT = "ccreport-form"
#: The only shape of referer path that carries information worth keeping: which
#: month the user was looking at. The period is re-matched, never passed through.
_REPORT_PATH_RE = re.compile(r"^/reports/(?P<period>\d{4}-\d{2})(?:/.*)?$")
CSRF_MAX_AGE_SECONDS = 12 * 3600


# ----------------------------------------------------------------- CSRF token
def _csrf_signer(settings: Settings) -> StateSigner | None:
    if not settings.session_secret:
        return None
    return StateSigner.from_settings(settings, salt=_CSRF_SALT)


def issue_csrf(request: Request, upn: str) -> str:
    signer = request.app.state.csrf
    if signer is None:
        return ""
    return signer.sign({"upn": upn, "purpose": "form"})


def check_csrf(request: Request, upn: str, token: str | None) -> None:
    """Reject a POST that did not come from a form we rendered for this user.

    Easy Auth authenticates the browser, not the intent: without this, any site
    the user visits could post to ``/reports/2026-07/submit`` on their behalf.
    """
    signer = request.app.state.csrf
    if signer is None:
        return
    if not token:
        raise HTTPException(status_code=400, detail="missing form token; reload the page and retry")
    try:
        payload = signer.unsign(token, max_age=CSRF_MAX_AGE_SECONDS)
    except CCReportError as exc:
        # A bad or stale token is a rejected request, not an application error to
        # redirect through: answer 400 so the failure is unambiguous.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if payload.get("upn") != upn or payload.get("purpose") != "form":
        raise HTTPException(status_code=400, detail="this form token was issued for another session")


# ---------------------------------------------------------------- dependencies
def get_settings_dep(request: Request) -> Settings:
    """The settings this app was built with.

    Routes read them from application state rather than the process-wide cache,
    so an app constructed with explicit settings behaves the way it was
    constructed — including in tests, where the two would otherwise disagree.
    """
    return getattr(request.app.state, "settings", None) or get_settings()


def db_session(request: Request) -> Iterator[Session]:
    factory = request.app.state.sessionmaker
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def current_principal(request: Request, settings: Settings = Depends(get_settings_dep)) -> Principal:
    return resolve_principal(request.headers, settings=settings)


def current_user(
    request: Request,
    session: Session = Depends(db_session),
    principal: Principal = Depends(current_principal),
    settings: Settings = Depends(get_settings_dep),
) -> User:
    user = authorize(session, principal, settings)
    request.state.principal = principal
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    from ..auth.allowlist import InsufficientRole, has_role

    if not has_role(user, ROLE_ADMIN):
        raise InsufficientRole(
            f"{user.upn} is not an administrator. Ask an existing administrator to grant the role."
        )
    return user


# ---------------------------------------------------------------------- helpers
def _connector_for(request: Request, session: Session, account: MailAccount, settings: Settings):
    """Build a connector, through the app's factory.

    The indirection exists so the test suite can hand in in-process fakes
    without the application growing a "test mode" that could ship enabled.
    """
    factory = getattr(request.app.state, "connector_factory", None)
    if factory is not None:
        return factory(session, account, settings)
    return open_connector(session, account, settings=settings)


def _return_to(request: Request) -> str:
    """Where to send a browser after a failed request.

    The ``Referer`` header is chosen by the client, so it is used to *choose
    among our own pages*, never as a destination. Every value returned here is a
    literal in this function — at most with a ``YYYY-MM`` period substituted in —
    so no character an attacker controls can reach the ``Location`` header.

    Filtering the string instead was not enough. A referer path of ``/\\evil.com``
    survives an "it starts with a single slash" check and is then treated by
    browsers as scheme-relative, which is an open redirect with extra steps.

    A referer pointing at the page that just failed is discarded too: sending a
    failed GET back to itself is a redirect loop the browser abandons long after
    the user has.
    """
    referer = urlsplit(request.headers.get("referer") or "")
    if referer.netloc and referer.netloc != request.url.netloc:
        return "/"  # another site sent them here; it does not choose where they go next
    path = referer.path
    if path == request.url.path:
        return "/"

    match = _REPORT_PATH_RE.match(path)
    if match:
        return f"/reports/{match.group('period')}"
    if path.startswith("/accounts"):
        return "/accounts"
    if path.startswith("/admin"):
        return "/admin"
    return "/"


def _report_url(period: str) -> str:
    """The URL of a month's page, rebuilt from integers.

    The ``period`` in a route is a URL segment, which is to say a string the
    caller chose. Interpolating it straight back into a redirect puts caller
    text in a ``Location`` header; parsing it and reformatting from the parsed
    year and month cannot.
    """
    year, month = parse_period(period)
    return f"/reports/{format_period(year, month)}"


def _redirect(url: str, *, message: str | None = None, error: str | None = None) -> RedirectResponse:
    from urllib.parse import quote

    parts = []
    if message:
        parts.append(f"msg={quote(message)}")
    if error:
        parts.append(f"err={quote(error)}")
    joined = ("&" if "?" in url else "?").join([url, "&".join(parts)]) if parts else url
    return RedirectResponse(joined, status_code=303)


def _context(request: Request, user: User, settings: Settings, **extra: Any) -> dict:
    principal = getattr(request.state, "principal", None)
    return {
        "request": request,
        "user": user,
        "settings": settings,
        "version": __version__,
        "is_admin": user.role == ROLE_ADMIN,
        "is_dev_principal": bool(principal and principal.is_dev),
        "periods": available_periods(settings.month_window),
        "csrf_token": issue_csrf(request, user.upn),
        "message": request.query_params.get("msg"),
        "error": request.query_params.get("err"),
        **extra,
    }


# ------------------------------------------------------------------ the app
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """Reconcile the seeded allow-list once, at boot.

        A deploy that changes ``CCREPORT_ALLOWED_PRINCIPALS`` should take effect
        on restart without anyone running a command. A database that is not up
        yet must not stop the container from booting — the health endpoint has
        to answer before App Service will route traffic at all.
        """
        try:
            with app.state.sessionmaker() as session:
                changed = bootstrap_allow_list(session, settings)
                session.commit()
            if changed:
                logger.info("allow-list reconciled from settings: %d change(s)", changed)
        except Exception as exc:
            logger.warning("could not reconcile the allow-list at startup: %s", exc)
        yield

    app = FastAPI(
        title="ccreport",
        version=__version__,
        description="Monthly receipt collection for faculty.",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.sessionmaker = get_sessionmaker(settings)
    app.state.csrf = _csrf_signer(settings)

    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    templates.env.filters["period_label"] = lambda p: period_label(int(p[:4]), int(p[5:7]))
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    @app.exception_handler(PrincipalError)
    async def _principal_error(request: Request, exc: PrincipalError) -> Response:
        status = 401 if not isinstance(exc, InconsistentPrincipal) else 403
        return templates.TemplateResponse(
            request=request,
            name="denied.html",
            context={"request": request, "title": "Not signed in", "detail": str(exc), "version": __version__},
            status_code=status,
        )

    @app.exception_handler(NotAuthorized)
    async def _not_authorized(request: Request, exc: NotAuthorized) -> Response:
        return templates.TemplateResponse(
            request=request,
            name="denied.html",
            context={"request": request, "title": "Access denied", "detail": str(exc), "version": __version__},
            status_code=403,
        )

    @app.exception_handler(CCReportError)
    async def _app_error(request: Request, exc: CCReportError) -> Response:
        logger.info("request failed: %s", exc)
        return _redirect(_return_to(request), error=str(exc))

    # ------------------------------------------------------------- health
    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        """Liveness only. Deliberately says nothing about configuration."""
        return {"status": "ok", "version": __version__}

    # ---------------------------------------------------------- dashboard
    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        session: Session = Depends(db_session),
        user: User = Depends(current_user),
        settings: Settings = Depends(get_settings_dep),
    ) -> Response:
        accounts = list_accounts(session, user)
        reports = list_reports(session, user)
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context=_context(
                request,
                user,
                settings,
                accounts=[account_summary(a) for a in accounts],
                reports=[report_summary(r, include_items=False) for r in reports],
            ),
        )

    # ----------------------------------------------------------- accounts
    @app.get("/accounts", response_class=HTMLResponse)
    def accounts_page(
        request: Request,
        session: Session = Depends(db_session),
        user: User = Depends(current_user),
        settings: Settings = Depends(get_settings_dep),
    ) -> Response:
        return templates.TemplateResponse(
            request=request,
            name="accounts.html",
            context=_context(
                request,
                user,
                settings,
                accounts=[account_summary(a) for a in list_accounts(session, user)],
                posture=connector_posture(settings),
            ),
        )

    @app.get("/accounts/connect/{provider}")
    def accounts_connect(
        provider: str,
        request: Request,
        user: User = Depends(current_user),
        settings: Settings = Depends(get_settings_dep),
    ) -> Response:
        client = build_oauth_client(provider, settings)
        verifier = new_code_verifier()
        state = StateSigner.from_settings(settings).sign(
            {"upn": user.upn, "provider": provider, "verifier": verifier}
        )
        return RedirectResponse(
            client.authorization_url(
                redirect_uri=redirect_uri_for(provider, settings),
                state=state,
                code_challenge=code_challenge_for(verifier),
            ),
            status_code=303,
        )

    @app.get("/oauth/callback/{provider}")
    def oauth_callback(
        provider: str,
        request: Request,
        session: Session = Depends(db_session),
        user: User = Depends(current_user),
        settings: Settings = Depends(get_settings_dep),
        code: str | None = Query(default=None),
        state: str | None = Query(default=None),
        error: str | None = Query(default=None),
        error_description: str | None = Query(default=None),
    ) -> Response:
        if error:
            return _redirect("/accounts", error=f"{provider} declined: {error_description or error}")
        if not code or not state:
            return _redirect("/accounts", error="the authorization response was incomplete")

        payload = StateSigner.from_settings(settings).unsign(state)
        if payload.get("upn") != user.upn or payload.get("provider") != provider:
            raise NotAuthorized("this authorization was started by a different user")

        client = build_oauth_client(provider, settings)
        tokens = client.exchange_code(
            code,
            redirect_uri=redirect_uri_for(provider, settings),
            code_verifier=payload["verifier"],
        )
        account = connect_oauth_account(session, user, provider, tokens, settings=settings)
        return _redirect("/accounts", message=f"connected {account.address}")

    @app.post("/accounts/imap")
    def accounts_connect_imap(
        request: Request,
        session: Session = Depends(db_session),
        user: User = Depends(current_user),
        settings: Settings = Depends(get_settings_dep),
        address: str = Form(...),
        app_password: str = Form(...),
        host: str | None = Form(default=None),
        port: int | None = Form(default=None),
        csrf_token: str = Form(default=""),
    ) -> Response:
        check_csrf(request, user.upn, csrf_token)
        account = connect_imap_account(
            session, user, address, app_password, host=host or None, port=port or None, settings=settings
        )
        return _redirect("/accounts", message=f"connected {account.address}")

    @app.post("/accounts/{account_id}/test")
    def accounts_test(
        account_id: str,
        request: Request,
        session: Session = Depends(db_session),
        user: User = Depends(current_user),
        settings: Settings = Depends(get_settings_dep),
        csrf_token: str = Form(default=""),
    ) -> Response:
        check_csrf(request, user.upn, csrf_token)
        account = get_account(session, user, account_id)
        status = verify_account(session, user, account, settings=settings)
        if status.ok:
            note = f"{account.address} is reachable"
            if status.warnings:
                note += f" — {status.warnings[0]}"
            return _redirect("/accounts", message=note)
        return _redirect("/accounts", error=f"{account.address}: {status.detail}")

    @app.post("/accounts/{account_id}/disconnect")
    def accounts_disconnect(
        account_id: str,
        request: Request,
        session: Session = Depends(db_session),
        user: User = Depends(current_user),
        csrf_token: str = Form(default=""),
    ) -> Response:
        check_csrf(request, user.upn, csrf_token)
        account = get_account(session, user, account_id)
        address = account.address
        disconnect_account(session, user, account_id)
        return _redirect("/accounts", message=f"disconnected {address}; the stored credential was deleted")

    @app.get("/accounts/{account_id}/folders")
    def accounts_folders(
        account_id: str,
        request: Request,
        session: Session = Depends(db_session),
        user: User = Depends(current_user),
        settings: Settings = Depends(get_settings_dep),
    ) -> dict:
        account = get_account(session, user, account_id)
        return {"folders": list_folders(_connector_for(request, session, account, settings))}

    # ------------------------------------------------------------ browsing
    @app.get("/reports/{period}", response_class=HTMLResponse)
    def report_page(
        period: str,
        request: Request,
        session: Session = Depends(db_session),
        user: User = Depends(current_user),
        settings: Settings = Depends(get_settings_dep),
        account_id: str | None = Query(default=None),
        subject: str | None = Query(default=None),
        sender: str | None = Query(default=None),
        show: str = Query(default="receipts"),
    ) -> Response:
        # A month is validated here rather than deep inside browse, so an
        # out-of-window URL produces one clear page instead of an error
        # redirected back to the page that raised it.
        clamp_period(period, settings.month_window)

        accounts = list_accounts(session, user)
        # Looking at a month must not create anything. A draft appears when the
        # first receipt is selected; otherwise every month anyone clicked would
        # show up on their dashboard as an empty draft.
        report = find_report(session, user, period)

        selected = None
        result = None
        warning = None
        if accounts:
            selected = next((a for a in accounts if a.id == account_id), accounts[0])
            try:
                result = browse(
                    session,
                    user,
                    selected,
                    period,
                    connector=_connector_for(request, session, selected, settings),
                    subject_contains=subject or None,
                    from_contains=sender or None,
                    receipts_only=show != "all",
                    settings=settings,
                )
            except (ReauthRequired, ConfigError) as exc:
                warning = str(exc)
            except CCReportError as exc:
                warning = str(exc)

        summary = report_summary(report) if report is not None else empty_report_summary(period)
        chosen = {item["message_id"] for item in summary["receipts"]}
        return templates.TemplateResponse(
            request=request,
            name="report.html",
            context=_context(
                request,
                user,
                settings,
                period=period,
                period_title=period_label(int(period[:4]), int(period[5:7])),
                accounts=[account_summary(a) for a in accounts],
                selected_account=account_summary(selected) if selected else None,
                browse=result.as_dict() if result else None,
                chosen=chosen,
                report=summary,
                filters={"subject": subject or "", "sender": sender or "", "show": show},
                warning=warning,
            ),
        )

    @app.post("/reports/{period}/items")
    def report_add_item(
        period: str,
        request: Request,
        session: Session = Depends(db_session),
        user: User = Depends(current_user),
        settings: Settings = Depends(get_settings_dep),
        account_id: str = Form(...),
        message_id: str = Form(...),
        csrf_token: str = Form(default=""),
    ) -> Response:
        check_csrf(request, user.upn, csrf_token)
        account = get_account(session, user, account_id)
        report = create_report(session, user, period, settings=settings)
        header = find_header(
            session, user, account, period, message_id,
            connector=_connector_for(request, session, account, settings), settings=settings,
        )
        if header is None:
            return _redirect(_report_url(period), error="that message is no longer in this month")
        add_item(session, user, report, account, header)
        return _redirect(_report_url(period), message="added; a justification is still needed")

    @app.post("/reports/{period}/items/{item_id}/justify")
    def report_justify(
        period: str,
        item_id: str,
        request: Request,
        session: Session = Depends(db_session),
        user: User = Depends(current_user),
        text: str = Form(...),
        csrf_token: str = Form(default=""),
    ) -> Response:
        check_csrf(request, user.upn, csrf_token)
        report = get_report(session, user, period)
        justify_item(session, user, report, item_id, text)
        return _redirect(_report_url(period), message="justification saved")

    @app.post("/reports/{period}/items/{item_id}/remove")
    def report_remove(
        period: str,
        item_id: str,
        request: Request,
        session: Session = Depends(db_session),
        user: User = Depends(current_user),
        csrf_token: str = Form(default=""),
    ) -> Response:
        check_csrf(request, user.upn, csrf_token)
        report = get_report(session, user, period)
        remove_item(session, user, report, item_id)
        return _redirect(_report_url(period), message="removed")

    @app.post("/reports/{period}/submit")
    def report_submit(
        period: str,
        request: Request,
        session: Session = Depends(db_session),
        user: User = Depends(current_user),
        settings: Settings = Depends(get_settings_dep),
        csrf_token: str = Form(default=""),
    ) -> Response:
        check_csrf(request, user.upn, csrf_token)
        report = get_report(session, user, period)
        submit_report(
            session,
            user,
            report,
            open_connector=lambda account: _connector_for(request, session, account, settings),
            settings=settings,
        )
        return _redirect(_report_url(period), message="submitted; the bundle is ready to download")

    @app.get("/reports/{period}/bundle.zip")
    def report_bundle(
        period: str,
        session: Session = Depends(db_session),
        user: User = Depends(current_user),
        settings: Settings = Depends(get_settings_dep),
    ) -> Response:
        report = get_report(session, user, period)
        filename, content = export_bundle(session, report, user, settings=settings)
        return Response(
            content=content,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # --------------------------------------------------------------- admin
    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(
        request: Request,
        session: Session = Depends(db_session),
        user: User = Depends(require_admin),
        settings: Settings = Depends(get_settings_dep),
        upn: str | None = Query(default=None),
        month: str | None = Query(default=None),
    ) -> Response:
        rows = admin_list_reports(session, user, upn=upn, period=month)
        return templates.TemplateResponse(
            request=request,
            name="admin.html",
            context=_context(
                request,
                user,
                settings,
                rows=[
                    {"user": owner.upn, **report_summary(report, include_items=False)}
                    for owner, report in rows
                ],
                allowed=[
                    {"upn": row.upn, "role": row.role, "seeded": row.seeded, "note": row.note}
                    for row in list_access(session)
                ],
                filters={"upn": upn or "", "month": month or ""},
            ),
        )

    @app.get("/admin/reports/{upn}/{period}/bundle.zip")
    def admin_bundle(
        upn: str,
        period: str,
        session: Session = Depends(db_session),
        user: User = Depends(require_admin),
        settings: Settings = Depends(get_settings_dep),
    ) -> Response:
        rows = admin_list_reports(session, user, upn=upn, period=period)
        if not rows:
            raise HTTPException(status_code=404, detail=f"no submitted report for {upn} in {period}")
        owner, report = rows[0]
        filename, content = export_bundle(session, report, owner, settings=settings, actor=user)
        return Response(
            content=content,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/admin/allow")
    def admin_allow(
        request: Request,
        session: Session = Depends(db_session),
        user: User = Depends(require_admin),
        action: str = Form(...),
        upn: str = Form(...),
        role: str = Form(default=ROLE_FACULTY),
        note: str | None = Form(default=None),
        csrf_token: str = Form(default=""),
    ) -> Response:
        check_csrf(request, user.upn, csrf_token)
        if action == "remove":
            if upn.strip().lower() == user.upn:
                return _redirect("/admin", error="removing your own access would lock you out")
            removed = revoke_access(session, upn, removed_by=user.upn)
            return _redirect(
                "/admin",
                message=f"removed {upn}" if removed else None,
                error=None if removed else f"{upn} was not on the list",
            )
        grant_access(session, upn, role=role, added_by=user.upn, note=note)
        return _redirect("/admin", message=f"added {upn} as {role}")

    # ----------------------------------------------------------- posture API
    @app.get("/api/connectors/posture")
    def posture(
        user: User = Depends(current_user),
        settings: Settings = Depends(get_settings_dep),
    ) -> dict:
        """What can be connected right now, and what will go wrong if it is.

        Authenticated on purpose: it describes how the deployment is configured,
        which is not something to hand to an unauthenticated caller. Deployment
        verification treats a 401 here as a warning rather than a failure.
        """
        return connector_posture(settings)

    return app



app = create_app()
