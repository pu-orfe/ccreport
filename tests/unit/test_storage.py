from __future__ import annotations

import pytest

from ccreport.settings import Settings
from ccreport.storage import (
    LocalArtifactStore,
    StorageError,
    artifact_path,
    get_artifact_store,
    safe_filename,
)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("receipt.pdf", "receipt.pdf"),
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\ada\\receipt.pdf", "receipt.pdf"),
        ("in voice #7.pdf", "in-voice-7.pdf"),
        # A slash is a separator wherever it appears, so only the last segment survives.
        ("invoice #7/2026.pdf", "2026.pdf"),
        ("...", "attachment"),
        ("", "attachment"),
        ("\U0001f9fe", "attachment"),
        ("a" * 200 + ".pdf", "a" * 64 + ".pdf"),
    ],
)
def test_provider_supplied_filenames_are_reduced_to_something_safe(given: str, expected: str) -> None:
    """Attachment names come from anyone who can email a faculty member."""
    assert safe_filename(given) == expected


def test_artifact_paths_are_generated_never_accepted() -> None:
    path = artifact_path("report-1", "artifact-2", "../escape.pdf")
    assert path == "reports/report-1/artifact-2/escape.pdf"


def test_round_trip_and_delete(store: LocalArtifactStore) -> None:
    path = store.put("reports/r1/a1/receipt.pdf", b"%PDF-1.4", content_type="application/pdf")
    assert store.exists(path)
    assert store.get(path) == b"%PDF-1.4"
    assert store.delete(path) is True
    assert store.delete(path) is False
    assert not store.exists(path)


def test_missing_artifact_raises_rather_than_returning_empty(store: LocalArtifactStore) -> None:
    with pytest.raises(StorageError, match="not found"):
        store.get("reports/r1/a1/absent.pdf")


def test_delete_prefix_removes_a_whole_report(store: LocalArtifactStore) -> None:
    store.put("reports/r1/a1/one.pdf", b"1")
    store.put("reports/r1/a2/two.pdf", b"2")
    store.put("reports/r2/a3/three.pdf", b"3")

    assert store.delete_prefix("reports/r1") == 2
    assert store.exists("reports/r2/a3/three.pdf")


@pytest.mark.parametrize("path", ["../outside.pdf", "/etc/passwd", "reports/../../escape"])
def test_traversal_is_refused_by_the_store_itself(store: LocalArtifactStore, path: str) -> None:
    with pytest.raises(StorageError):
        store.put(path, b"x")


def test_local_store_is_the_default_and_blob_takes_over_when_configured(tmp_path) -> None:
    from ccreport.storage import BlobArtifactStore

    local = get_artifact_store(Settings(local_artifact_dir=str(tmp_path / "art")))
    assert isinstance(local, LocalArtifactStore)

    blob = get_artifact_store(
        Settings(blob_account_url="https://acct.blob.core.windows.net", blob_container="bundle")
    )
    assert isinstance(blob, BlobArtifactStore)
    assert blob.container == "bundle"
