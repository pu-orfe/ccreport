from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ccreport import cli
from ccreport.models import Base

FACULTY = "ada@princeton.edu"
ADMIN = "grace@princeton.edu"


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A CLI whose database is a file, so each invocation sees the last one's work."""
    database_url = f"sqlite:///{tmp_path / 'ccreport.db'}"
    monkeypatch.setenv("CCREPORT_ENVIRONMENT", "test")
    monkeypatch.setenv("CCREPORT_DATABASE_URL", database_url)
    monkeypatch.setenv("CCREPORT_ALLOWED_PRINCIPALS", f"{FACULTY},{ADMIN}")
    monkeypatch.setenv("CCREPORT_ADMIN_PRINCIPALS", ADMIN)
    monkeypatch.setenv("CCREPORT_SESSION_SECRET", "test-only-session-secret")
    monkeypatch.setenv("CCREPORT_LOCAL_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("CCREPORT_BUNDLE_DIR", str(tmp_path / "bundles"))
    from ccreport.settings import get_settings

    get_settings.cache_clear()
    Base.metadata.create_all(create_engine(database_url))
    sessionmaker(bind=create_engine(database_url))


def run(capsys, *argv: str) -> tuple[int, object, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    try:
        payload = json.loads(captured.out) if captured.out.strip() else None
    except json.JSONDecodeError:
        payload = captured.out
    return code, payload, captured.err


# ------------------------------------------------------------------- plumbing
def test_version_is_printed_without_touching_anything(capsys) -> None:
    from ccreport import __version__

    code, out, _ = run(capsys, "--version")
    assert code == 0
    assert out is None or str(out).strip() == __version__


def test_no_arguments_prints_usage_to_stderr(capsys) -> None:
    code, out, err = run(capsys)
    assert code == cli.EXIT_USAGE
    assert out is None
    assert "usage: ccreport" in err


def test_global_flags_are_accepted_after_the_subcommand(configured, capsys) -> None:
    code, out, _ = run(capsys, "account", "list", "--principal", FACULTY, "--output", "text")
    assert code == 0
    assert out == "(none)\n"


def test_stdout_is_json_and_diagnostics_go_to_stderr(configured, capsys) -> None:
    code, out, err = run(capsys, "-P", FACULTY, "-v", "account", "list")
    assert code == 0
    assert out == []
    assert "{" not in err


def test_a_missing_principal_is_a_configuration_error(configured, capsys) -> None:
    code, _, err = run(capsys, "account", "list")
    assert code == cli.EXIT_CONFIG
    assert "--principal" in err


def test_the_allow_list_applies_to_the_cli_too(configured, capsys) -> None:
    code, _, err = run(capsys, "-P", "stranger@princeton.edu", "account", "list")
    assert code == cli.EXIT_DENIED
    assert "not on the ccreport access list" in err


def test_a_foreign_domain_is_refused_at_the_first_gate(configured, capsys) -> None:
    code, _, err = run(capsys, "-P", "ada@example.com", "account", "list")
    assert code == cli.EXIT_DENIED
    assert "outside princeton.edu" in err


# ---------------------------------------------------------------------- doctor
def test_doctor_reports_failures_with_a_non_zero_exit(capsys, monkeypatch) -> None:
    monkeypatch.setenv("CCREPORT_ENVIRONMENT", "test")
    code, out, _ = run(capsys, "doctor")
    assert code == cli.EXIT_ERROR
    assert out["status"] == "fail"
    assert any(c["name"] == "access.allow_list" for c in out["checks"])


def test_doctor_prints_a_usable_development_key(capsys) -> None:
    import base64

    code, out, _ = run(capsys, "doctor", "--generate-key")
    assert code == 0
    assert len(base64.b64decode(out["CCREPORT_DEV_ENCRYPTION_KEY"])) == 32


def test_doctor_needs_no_database_or_principal(capsys) -> None:
    code, _, _ = run(capsys, "doctor")
    assert code in (cli.EXIT_OK, cli.EXIT_ERROR)  # never a crash or a denial


# ----------------------------------------------------------------------- admin
def test_admin_commands_require_the_admin_role(configured, capsys) -> None:
    code, _, err = run(capsys, "-P", FACULTY, "admin", "allow", "list")
    assert code == cli.EXIT_DENIED
    assert "'admin' role" in err


def test_an_administrator_can_grant_and_revoke_access(configured, capsys) -> None:
    code, out, _ = run(capsys, "-P", ADMIN, "admin", "allow", "add", "newbie@princeton.edu")
    assert code == 0 and out["granted"] is True

    code, out, _ = run(capsys, "-P", ADMIN, "admin", "allow", "list")
    assert "newbie@princeton.edu" in [row["upn"] for row in out]

    code, out, _ = run(capsys, "-P", ADMIN, "admin", "allow", "remove", "newbie@princeton.edu")
    assert code == 0 and out["removed"] is True


def test_a_granted_principal_can_then_use_the_cli(configured, capsys) -> None:
    run(capsys, "-P", ADMIN, "admin", "allow", "add", "newbie@princeton.edu")
    code, out, _ = run(capsys, "-P", "newbie@princeton.edu", "account", "list")
    assert code == 0 and out == []


def test_removing_a_principal_that_was_never_there_is_reported(configured, capsys) -> None:
    code, out, _ = run(capsys, "-P", ADMIN, "admin", "allow", "remove", "ghost@princeton.edu")
    assert code == cli.EXIT_ERROR
    assert out["removed"] is False


# --------------------------------------------------------------------- reports
def test_report_lifecycle_over_several_invocations(configured, capsys) -> None:
    from ccreport.period import available_periods

    period = available_periods(2)[1]

    code, out, _ = run(capsys, "-P", FACULTY, "report", "create", "--month", period)
    assert code == 0 and out["status"] == "draft" and out["items"] == 0

    code, out, _ = run(capsys, "-P", FACULTY, "report", "list")
    assert [r["period"] for r in out] == [period]

    code, out, _ = run(capsys, "-P", FACULTY, "report", "show", period)
    assert code == 0 and out["receipts"] == []


def test_a_month_outside_the_window_is_refused_by_the_cli(configured, capsys) -> None:
    code, _, err = run(capsys, "-P", FACULTY, "report", "create", "--month", "2020-01")
    assert code == cli.EXIT_ERROR
    assert "outside the permitted window" in err


def test_a_malformed_month_is_a_one_line_error_not_a_traceback(configured, capsys) -> None:
    code, _, err = run(capsys, "-P", FACULTY, "report", "show", "July")
    assert code == cli.EXIT_ERROR
    assert err.startswith("ccreport: ")
    assert "Traceback" not in err


def test_an_unknown_account_reference_is_refused(configured, capsys) -> None:
    code, _, err = run(
        capsys, "-P", FACULTY, "messages", "list", "--account", "nope", "--month",
        __import__("ccreport.period", fromlist=["x"]).available_periods(1)[0],
    )
    assert code == cli.EXIT_DENIED
    assert "no connected mailbox matches" in err


# --------------------------------------------------------------------- account
def test_connecting_imap_without_a_password_is_a_configuration_error(configured, capsys, monkeypatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    code, _, err = run(capsys, "-P", FACULTY, "account", "connect", "imap", "--address", "ada@gmail.com")
    assert code == cli.EXIT_CONFIG
    assert "app password" in err


def test_starting_an_oauth_authorization_prints_a_url_and_a_signed_state(
    configured, capsys, monkeypatch
) -> None:
    monkeypatch.setenv("CCREPORT_MS_CLIENT_ID", "ms-client")
    monkeypatch.setenv("CCREPORT_MS_CLIENT_SECRET", "ms-secret")
    from ccreport.settings import get_settings

    get_settings.cache_clear()

    code, out, err = run(capsys, "-P", FACULTY, "account", "connect", "graph")
    assert code == 0
    assert out["authorization_url"].startswith("https://login.microsoftonline.com/")
    assert "code_challenge_method=S256" in out["authorization_url"]
    assert out["state"]
    assert "Open this URL" in err


def test_completing_an_authorization_with_a_foreign_state_is_refused(configured, capsys, monkeypatch) -> None:
    monkeypatch.setenv("CCREPORT_MS_CLIENT_ID", "ms-client")
    monkeypatch.setenv("CCREPORT_MS_CLIENT_SECRET", "ms-secret")
    from ccreport.oauth import StateSigner
    from ccreport.settings import get_settings

    get_settings.cache_clear()
    foreign = StateSigner("test-only-session-secret").sign(
        {"upn": "someone@princeton.edu", "provider": "graph", "verifier": "v"}
    )
    code, _, err = run(
        capsys, "-P", FACULTY, "account", "connect", "graph", "--code", "c", "--state", foreign
    )
    assert code == cli.EXIT_DENIED
    assert "different user" in err


# -------------------------------------------------------------------------- db
def test_db_upgrade_runs_migrations_without_a_principal(capsys, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CCREPORT_ENVIRONMENT", "test")
    monkeypatch.setenv("CCREPORT_DATABASE_URL", f"sqlite:///{tmp_path / 'migrated.db'}")
    from ccreport.settings import get_settings

    get_settings.cache_clear()

    code, out, _ = run(capsys, "db", "upgrade")
    assert code == 0
    assert out["upgraded_to"] == "head"

    from sqlalchemy import create_engine, inspect

    tables = set(inspect(create_engine(f"sqlite:///{tmp_path / 'migrated.db'}")).get_table_names())
    assert {"reports", "report_items", "artifacts", "alembic_version"} <= tables


def test_database_urls_are_redacted_in_output(capsys, monkeypatch, tmp_path) -> None:
    assert cli._redact_url("postgresql+psycopg://user:hunter2@host:5432/db") == (
        "postgresql+psycopg://user:***@host:5432/db"
    )
    assert cli._redact_url("sqlite:///local.db") == "sqlite:///local.db"


def test_create_all_is_refused_in_production(capsys, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("CCREPORT_ENVIRONMENT", "production")
    monkeypatch.setenv("CCREPORT_DATABASE_URL", f"sqlite:///{tmp_path / 'prod.db'}")
    monkeypatch.setenv("CCREPORT_SESSION_SECRET", "test-only-session-secret")
    from ccreport.settings import get_settings

    get_settings.cache_clear()

    code, _, err = run(capsys, "db", "create-all")
    assert code == cli.EXIT_CONFIG
    assert "reviewed migration" in err


# ------------------------------------------------------------------ formatting
def test_text_output_renders_nested_structures_readably() -> None:
    rendered = cli._as_text({"period": "2026-07", "receipts": [{"index": 1, "vendor": "Amazon"}]})
    assert "period: 2026-07" in rendered
    assert "  index: 1" in rendered
    assert "  vendor: Amazon" in rendered
