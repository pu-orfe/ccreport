from __future__ import annotations

import base64
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from tests.conftest import ADMIN_UPN, FACULTY_UPN, TEST_DEK
from tests.fakes.connector import PDF_REF, FakeConnector, header

from ccreport.models import Base, MailAccount, User
from ccreport.period import available_periods, parse_period
from ccreport.settings import Settings
from ccreport.web.app import create_app

#: Last month, so the fixture stays inside the month window whenever it is run.
PERIOD = available_periods(2)[1]
YEAR, MONTH = parse_period(PERIOD)


def easy_auth_headers(upn: str, *, name: str = "Ada Lovelace", claim_upn: str | None = None) -> dict:
    """The headers Easy Auth sets on an authenticated request."""
    document = {
        "auth_typ": "aad",
        "name_typ": "preferred_username",
        "claims": [
            {"typ": "preferred_username", "val": claim_upn or upn},
            {"typ": "name", "val": name},
        ],
    }
    return {
        "X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(document).encode()).decode(),
        "X-MS-CLIENT-PRINCIPAL-NAME": upn,
        "X-MS-CLIENT-PRINCIPAL-IDP": "aad",
    }


@pytest.fixture
def connector() -> FakeConnector:
    return FakeConnector(
        [
            header(
                "m1", "Amazon.com order receipt for $42.50",
                year=YEAR, month=MONTH, day=3, attachments=(PDF_REF,),
            ),
            header("m2", "Faculty meeting Thursday", year=YEAR, month=MONTH, day=9, snippet="agenda"),
        ],
        attachments={("m1", "att-pdf"): b"%PDF-1.4 amazon\n"},
    )


@pytest.fixture
def app_settings(tmp_path) -> Settings:
    return Settings(
        environment="test",
        database_url=f"sqlite:///{tmp_path / 'web.db'}",
        allowed_principals=f"{FACULTY_UPN},{ADMIN_UPN}",
        admin_principals=ADMIN_UPN,
        session_secret="test-only-session-secret",
        dev_encryption_key=TEST_DEK,
        local_artifact_dir=str(tmp_path / "artifacts"),
        base_url="https://ccreport.example.edu",
    )


@pytest.fixture
def client(app_settings: Settings, connector: FakeConnector) -> TestClient:
    engine = create_engine(app_settings.database_url, future=True)
    Base.metadata.create_all(engine)

    app = create_app(app_settings)
    app.state.sessionmaker = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    app.state.connector_factory = lambda session, account, settings: connector

    with TestClient(app) as test_client:
        test_client.app_settings = app_settings
        test_client.engine = engine
        yield test_client


def connect_mailbox(client: TestClient, upn: str = FACULTY_UPN) -> MailAccount:
    # The User row is created by the first authenticated request, never by hand.
    client.get("/", headers=easy_auth_headers(upn))
    with client.app.state.sessionmaker() as session:
        user = session.query(User).filter_by(upn=upn).one()
        account = MailAccount(
            user_id=user.id, provider="graph", address=upn, status="connected"
        )
        session.add(account)
        session.commit()
        return account


# ------------------------------------------------------------------- the gates
def test_health_needs_no_authentication(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_an_unauthenticated_request_is_never_answered_with_200(client: TestClient) -> None:
    """Deployment verification refuses to call a deploy verified without this."""
    response = client.get("/")
    assert response.status_code == 401
    assert "Not signed in" in response.text


def test_a_name_header_without_a_claims_blob_is_refused(client: TestClient) -> None:
    response = client.get("/", headers={"X-MS-CLIENT-PRINCIPAL-NAME": FACULTY_UPN})
    assert response.status_code == 401
    assert "Easy Auth" in response.text


def test_a_name_that_disagrees_with_the_claims_is_refused(client: TestClient) -> None:
    headers = easy_auth_headers(FACULTY_UPN, claim_upn="someone.else@princeton.edu")
    response = client.get("/", headers=headers)
    assert response.status_code == 403


def test_a_principal_outside_the_domain_is_refused(client: TestClient) -> None:
    response = client.get("/", headers=easy_auth_headers("ada@example.com"))
    assert response.status_code == 403
    assert "institutional accounts" in response.text


def test_a_principal_not_on_the_allow_list_is_told_what_to_do(client: TestClient) -> None:
    response = client.get("/", headers=easy_auth_headers("stranger@princeton.edu"))
    assert response.status_code == 403
    assert "administrator must add you" in response.text


def test_the_allow_list_is_seeded_from_settings_at_startup(client: TestClient) -> None:
    from ccreport.models import AllowedPrincipal

    with client.app.state.sessionmaker() as session:
        assert {row.upn for row in session.query(AllowedPrincipal).all()} == {
            FACULTY_UPN,
            ADMIN_UPN,
        }


# --------------------------------------------------------------------- pages
def test_the_dashboard_lists_the_month_window(client: TestClient) -> None:
    response = client.get("/", headers=easy_auth_headers(FACULTY_UPN))
    assert response.status_code == 200
    assert "Your months" in response.text
    assert "Connect one" in response.text  # no mailbox connected yet


def test_the_accounts_page_describes_each_provider(client: TestClient) -> None:
    response = client.get("/accounts", headers=easy_auth_headers(FACULTY_UPN))
    assert response.status_code == 200
    assert "IMAP app password" in response.text
    assert "Microsoft Outlook" in response.text
    assert "does not give it access to any mailbox" in response.text


def test_browsing_shows_scored_messages_and_highlights_receipts(client: TestClient) -> None:
    connect_mailbox(client)
    response = client.get(f"/reports/{PERIOD}", headers=easy_auth_headers(FACULTY_UPN))
    assert response.status_code == 200
    assert "Amazon.com order receipt" in response.text
    assert "Faculty meeting" not in response.text  # highlighted view, by default

    everything = client.get(f"/reports/{PERIOD}?show=all", headers=easy_auth_headers(FACULTY_UPN))
    assert "Faculty meeting" in everything.text


# ----------------------------------------------------------------------- CSRF
def csrf_token(client: TestClient, upn: str = FACULTY_UPN) -> str:
    import re

    page = client.get("/accounts", headers=easy_auth_headers(upn)).text
    return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)


def test_a_post_without_a_form_token_is_refused(client: TestClient) -> None:
    account = connect_mailbox(client)
    response = client.post(
        f"/accounts/{account.id}/disconnect", headers=easy_auth_headers(FACULTY_UPN), data={}
    )
    assert response.status_code == 400


def test_an_oauth_state_cannot_be_replayed_as_a_form_token(client: TestClient) -> None:
    """Different purposes, different salts: neither token is valid as the other."""
    from ccreport.oauth import StateSigner

    account = connect_mailbox(client)
    oauth_state = StateSigner.from_settings(client.app.state.settings).sign(
        {"upn": FACULTY_UPN, "provider": "graph", "verifier": "v"}
    )
    response = client.post(
        f"/accounts/{account.id}/disconnect",
        headers=easy_auth_headers(FACULTY_UPN),
        data={"csrf_token": oauth_state},
        follow_redirects=False,
    )
    assert response.status_code == 400
    with client.app.state.sessionmaker() as session:
        assert session.get(MailAccount, account.id) is not None  # nothing happened


def test_a_form_token_issued_for_another_person_is_refused(client: TestClient) -> None:
    account = connect_mailbox(client)
    theirs = csrf_token(client, ADMIN_UPN)
    response = client.post(
        f"/accounts/{account.id}/disconnect",
        headers=easy_auth_headers(FACULTY_UPN),
        data={"csrf_token": theirs},
    )
    assert response.status_code == 400


def submit_a_report(client: TestClient, justification: str = "Textbooks for ORF 405") -> str:
    """Select one receipt, justify it, and submit — the whole faculty path."""
    account = connect_mailbox(client)
    headers = easy_auth_headers(FACULTY_UPN)
    token = csrf_token(client)
    client.post(
        f"/reports/{PERIOD}/items",
        headers=headers,
        data={"csrf_token": token, "account_id": account.id, "message_id": "m1"},
    )
    with client.app.state.sessionmaker() as session:
        from ccreport.models import ReportItem

        item_id = session.query(ReportItem).one().id
    client.post(
        f"/reports/{PERIOD}/items/{item_id}/justify",
        headers=headers,
        data={"csrf_token": token, "text": justification},
    )
    client.post(f"/reports/{PERIOD}/submit", headers=headers, data={"csrf_token": token})
    return item_id


# ------------------------------------------------------------- the whole flow
def test_select_justify_submit_and_download(client: TestClient) -> None:
    account = connect_mailbox(client)
    headers = easy_auth_headers(FACULTY_UPN)
    token = csrf_token(client)

    added = client.post(
        f"/reports/{PERIOD}/items",
        headers=headers,
        data={"csrf_token": token, "account_id": account.id, "message_id": "m1"},
        follow_redirects=True,
    )
    assert added.status_code == 200
    assert "a justification is still needed" in added.text

    with client.app.state.sessionmaker() as session:
        from ccreport.models import ReportItem

        item_id = session.query(ReportItem).one().id

    justified = client.post(
        f"/reports/{PERIOD}/items/{item_id}/justify",
        headers=headers,
        data={"csrf_token": token, "text": "Textbooks for ORF 405"},
        follow_redirects=True,
    )
    assert "justification saved" in justified.text

    submitted = client.post(
        f"/reports/{PERIOD}/submit",
        headers=headers,
        data={"csrf_token": token},
        follow_redirects=True,
    )
    assert submitted.status_code == 200
    assert "Download the bundle" in submitted.text

    bundle = client.get(f"/reports/{PERIOD}/bundle.zip", headers=headers)
    assert bundle.status_code == 200
    assert bundle.headers["content-type"] == "application/zip"
    assert f"ccreport-ada-{PERIOD}.zip" in bundle.headers["content-disposition"]

    archive = zipfile.ZipFile(io.BytesIO(bundle.content))
    manifest = json.loads(archive.read("manifest.json"))
    assert manifest["items"][0]["justification"] == "Textbooks for ORF 405"
    assert archive.read("receipts/001-amazon.pdf") == b"%PDF-1.4 amazon\n"


def test_submitting_without_justifications_is_refused_and_says_why(client: TestClient) -> None:
    account = connect_mailbox(client)
    headers = easy_auth_headers(FACULTY_UPN)
    token = csrf_token(client)
    client.post(
        f"/reports/{PERIOD}/items",
        headers=headers,
        data={"csrf_token": token, "account_id": account.id, "message_id": "m1"},
    )
    response = client.post(
        f"/reports/{PERIOD}/submit",
        headers=headers,
        data={"csrf_token": token},
        follow_redirects=True,
    )
    assert "no justification" in response.text


def test_one_persons_report_is_not_reachable_by_another(client: TestClient) -> None:
    """The report route is scoped to the caller, not to the period in the URL."""
    submit_a_report(client)

    response = client.get(
        f"/reports/{PERIOD}/bundle.zip",
        headers=easy_auth_headers(ADMIN_UPN),
        follow_redirects=False,
    )
    assert response.status_code == 303  # no report of their own for that month
    from urllib.parse import unquote

    assert "no report for" in unquote(response.headers["location"])


# ----------------------------------------------------------------- regressions
def test_an_error_never_redirects_to_a_referer_off_this_site(client: TestClient) -> None:
    """The Referer is client-chosen; using it whole made every error an open redirect."""
    response = client.get(
        f"/reports/{PERIOD}/bundle.zip",
        headers={**easy_auth_headers(FACULTY_UPN), "Referer": "https://evil.example/steal"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/?err=")


def test_an_error_on_a_page_does_not_redirect_back_to_itself(client: TestClient) -> None:
    """A failed GET sent back to itself is a redirect loop the browser gives up on."""
    response = client.get(
        "/reports/2019-01",
        headers={
            **easy_auth_headers(FACULTY_UPN),
            "Referer": "https://ccreport.example.edu/reports/2019-01",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/?err=")


def test_viewing_a_month_creates_no_draft(client: TestClient) -> None:
    """Otherwise every month anybody clicked shows up as an empty draft."""
    from ccreport.models import Report

    connect_mailbox(client)
    assert client.get(f"/reports/{PERIOD}", headers=easy_auth_headers(FACULTY_UPN)).status_code == 200

    with client.app.state.sessionmaker() as session:
        assert session.query(Report).count() == 0

    dashboard = client.get("/", headers=easy_auth_headers(FACULTY_UPN))
    assert "not started" in dashboard.text


def test_selecting_the_first_receipt_is_what_creates_the_draft(client: TestClient) -> None:
    from ccreport.models import Report

    account = connect_mailbox(client)
    client.post(
        f"/reports/{PERIOD}/items",
        headers=easy_auth_headers(FACULTY_UPN),
        data={"csrf_token": csrf_token(client), "account_id": account.id, "message_id": "m1"},
    )
    with client.app.state.sessionmaker() as session:
        assert session.query(Report).count() == 1


# ---------------------------------------------------------------------- admin
def test_the_admin_console_is_closed_to_faculty(client: TestClient) -> None:
    response = client.get("/admin", headers=easy_auth_headers(FACULTY_UPN))
    assert response.status_code == 403
    assert "not an administrator" in response.text


def test_an_administrator_sees_submitted_reports_and_can_download_them(client: TestClient) -> None:
    submit_a_report(client)

    admin_headers = easy_auth_headers(ADMIN_UPN, name="Grace Hopper")
    console = client.get("/admin", headers=admin_headers)
    assert console.status_code == 200
    assert FACULTY_UPN in console.text

    bundle = client.get(
        f"/admin/reports/{FACULTY_UPN}/{PERIOD}/bundle.zip", headers=admin_headers
    )
    assert bundle.status_code == 200
    assert zipfile.ZipFile(io.BytesIO(bundle.content)).read("manifest.json")


def test_an_administrator_cannot_remove_their_own_access(client: TestClient) -> None:
    admin_headers = easy_auth_headers(ADMIN_UPN)
    client.get("/admin", headers=admin_headers)
    token = csrf_token(client, ADMIN_UPN)

    response = client.post(
        "/admin/allow",
        headers=admin_headers,
        data={"csrf_token": token, "action": "remove", "upn": ADMIN_UPN},
        follow_redirects=True,
    )
    assert "lock you out" in response.text


def test_an_administrator_can_add_somebody(client: TestClient) -> None:
    admin_headers = easy_auth_headers(ADMIN_UPN)
    token = csrf_token(client, ADMIN_UPN)
    client.post(
        "/admin/allow",
        headers=admin_headers,
        data={"csrf_token": token, "action": "add", "upn": "newbie@princeton.edu", "role": "faculty"},
    )
    assert client.get("/", headers=easy_auth_headers("newbie@princeton.edu")).status_code == 200


# ------------------------------------------------------------------- accounts
def test_disconnecting_removes_the_mailbox_and_its_credential(client: TestClient) -> None:
    account = connect_mailbox(client)
    response = client.post(
        f"/accounts/{account.id}/disconnect",
        headers=easy_auth_headers(FACULTY_UPN),
        data={"csrf_token": csrf_token(client)},
        follow_redirects=True,
    )
    assert "the stored credential was deleted" in response.text

    with client.app.state.sessionmaker() as session:
        assert session.get(MailAccount, account.id) is None


def test_the_posture_endpoint_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/connectors/posture").status_code == 401

    response = client.get("/api/connectors/posture", headers=easy_auth_headers(FACULTY_UPN))
    assert response.status_code == 200
    assert {p["provider"] for p in response.json()["providers"]} == {"graph", "gmail", "imap"}


def test_an_oauth_callback_with_a_forged_state_is_refused(client: TestClient) -> None:
    response = client.get(
        "/oauth/callback/graph?code=abc&state=forged",
        headers=easy_auth_headers(FACULTY_UPN),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "refusing+to+complete" in response.headers["location"].replace("%20", "+")
