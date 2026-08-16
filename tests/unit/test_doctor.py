from __future__ import annotations

from ccreport.doctor import connector_posture, diagnose
from ccreport.settings import Settings


def check(diagnosis, name: str):
    return next(c for c in diagnosis.checks if c.name == name)


def test_an_empty_allow_list_is_a_failure_not_a_warning() -> None:
    """Empty means deny-all, and a healthy-looking log would hide that."""
    diagnosis = diagnose(Settings(environment="test"))
    allow = check(diagnosis, "access.allow_list")
    assert allow.status == "fail"
    assert "denied" in allow.detail
    assert "admin allow add" in allow.remedy
    assert diagnosis.ok is False


def test_a_configured_deployment_reports_healthy(settings: Settings) -> None:
    diagnosis = diagnose(settings, allow_list_count=2)
    assert check(diagnosis, "access.allow_list").status == "ok"
    assert check(diagnosis, "secrets.session").status == "ok"
    assert check(diagnosis, "connector.graph").status == "ok"
    assert check(diagnosis, "connector.google").status == "ok"


def test_principals_granted_in_the_table_count_even_with_no_seed() -> None:
    diagnosis = diagnose(Settings(environment="test"), allow_list_count=5)
    allow = check(diagnosis, "access.allow_list")
    assert allow.status == "ok"
    assert "5 principal(s) in the allow-list table" in allow.detail


def test_a_seed_that_has_not_been_applied_yet_is_not_a_denial(settings: Settings) -> None:
    """The table is empty before first boot; the seed is applied at startup."""
    allow = check(diagnose(settings, allow_list_count=0), "access.allow_list")
    assert allow.status == "ok"
    assert "seeded from settings and applied at startup" in allow.detail


def test_a_non_durable_google_posture_warns_with_the_request_to_send(settings: Settings) -> None:
    diagnosis = diagnose(settings.model_copy(update={"google_oauth_publishing_status": "testing"}))
    google = check(diagnosis, "connector.google")
    assert google.status == "warn"
    assert "7 days" in google.detail
    assert "docs/OIT-REQUESTS.md" in google.remedy


def test_a_write_scope_on_the_graph_client_is_a_failure(settings: Settings) -> None:
    noisy = settings.model_copy(update={"ms_scopes": "offline_access Mail.ReadWrite"})
    graph = check(diagnose(noisy), "connector.graph")
    assert graph.status == "fail"
    assert "Mail.ReadWrite" in graph.remedy


def test_local_disk_storage_is_fine_locally_and_fatal_in_production(settings: Settings) -> None:
    assert check(diagnose(settings), "storage.artifacts").status == "ok"

    production = settings.model_copy(
        update={"environment": "production", "dev_encryption_key": None, "keyvault_url": "https://v/"}
    )
    artifacts = check(diagnose(production), "storage.artifacts")
    assert artifacts.status == "fail"
    assert "CCREPORT_BLOB_ACCOUNT_URL" in artifacts.remedy


def test_a_development_wrapping_key_is_fatal_in_production(settings: Settings) -> None:
    production = settings.model_copy(update={"environment": "production"})
    assert check(diagnose(production), "secrets.wrapping").status == "fail"


def test_no_wrapping_key_at_all_is_a_failure_with_a_way_out() -> None:
    wrapping = check(diagnose(Settings(environment="test")), "secrets.wrapping")
    assert wrapping.status == "fail"
    assert "--generate-key" in wrapping.remedy


def test_the_development_bypass_is_always_visible(settings: Settings) -> None:
    with_bypass = settings.model_copy(update={"dev_principal": "ada@princeton.edu"})
    bypass = check(diagnose(with_bypass), "access.dev_principal")
    assert bypass.status == "warn"
    assert "ada@princeton.edu" in bypass.detail


def test_diagnosis_serialises_with_counts(settings: Settings) -> None:
    data = diagnose(settings, allow_list_count=2).as_dict()
    assert set(data) == {"status", "ok", "counts", "checks"}
    assert data["counts"]["ok"] + data["counts"]["warn"] + data["counts"]["fail"] == len(
        data["checks"]
    )


def test_posture_describes_every_provider_and_the_gmail_expiry_risk(settings: Settings) -> None:
    posture = connector_posture(settings.model_copy(update={"google_oauth_publishing_status": "testing"}))
    providers = {p["provider"]: p for p in posture["providers"]}

    assert set(providers) == {"graph", "gmail", "imap"}
    assert providers["gmail"]["durable_refresh_tokens"] is False
    assert providers["gmail"]["personal_gmail_oauth"] is False
    assert providers["imap"]["configured"] is True
    assert all(c["name"].startswith("connector.") for c in posture["checks"])
