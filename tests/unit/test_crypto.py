from __future__ import annotations

import base64

import pytest
from cryptography.exceptions import InvalidTag

from ccreport.crypto import DevSecretBox, SealedSecret, generate_dev_key, get_secret_box
from ccreport.errors import ConfigError
from ccreport.settings import Settings

SECRET = "1//0erefresh-token-value"  # noqa: S105 — a fixture, not a credential


def test_sealed_secret_round_trips(secret_box: DevSecretBox) -> None:
    sealed = secret_box.seal(SECRET.encode())
    assert secret_box.open(sealed) == SECRET.encode()


def test_ciphertext_never_contains_the_plaintext(secret_box: DevSecretBox) -> None:
    sealed = secret_box.seal(SECRET.encode())
    assert SECRET.encode() not in sealed.ciphertext
    assert SECRET.encode() not in sealed.wrapped_dek


def test_each_seal_uses_a_fresh_data_key_and_nonce(secret_box: DevSecretBox) -> None:
    first = secret_box.seal(SECRET.encode())
    second = secret_box.seal(SECRET.encode())
    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext
    assert first.wrapped_dek != second.wrapped_dek


def test_tampered_ciphertext_is_rejected_rather_than_decrypted(secret_box: DevSecretBox) -> None:
    sealed = secret_box.seal(SECRET.encode())
    flipped = bytearray(sealed.ciphertext)
    flipped[0] ^= 0x01
    tampered = SealedSecret(
        wrapped_dek=sealed.wrapped_dek,
        key_name=sealed.key_name,
        key_version=sealed.key_version,
        nonce=sealed.nonce,
        ciphertext=bytes(flipped),
    )
    with pytest.raises(InvalidTag):
        secret_box.open(tampered)


def test_another_key_cannot_open_the_secret(secret_box: DevSecretBox) -> None:
    other = DevSecretBox(base64.b64decode(generate_dev_key()))
    with pytest.raises(InvalidTag):
        other.open(secret_box.seal(SECRET.encode()))


def test_settings_refuse_a_development_key_on_azure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first of two guards: the application will not even construct."""
    monkeypatch.setenv("WEBSITE_SITE_NAME", "ccreport-prod")
    with pytest.raises(ConfigError, match="wrapped by Key Vault"):
        Settings(dev_encryption_key=generate_dev_key())


def test_development_box_refuses_to_run_on_azure(monkeypatch: pytest.MonkeyPatch) -> None:
    """The second guard, in case settings were built before App Service was detected."""
    settings = Settings(dev_encryption_key=generate_dev_key(), keyvault_url=None)
    monkeypatch.setenv("WEBSITE_SITE_NAME", "ccreport-prod")
    with pytest.raises(ConfigError, match="refuses to run on Azure"):
        DevSecretBox.from_settings(settings)


def test_development_box_refuses_in_production() -> None:
    settings = Settings(
        environment="production",
        dev_encryption_key=generate_dev_key(),
        session_secret="test-only-session-secret",
    )
    with pytest.raises(ConfigError, match="refuses to run"):
        get_secret_box(settings)


def test_missing_key_is_a_configuration_error_not_a_silent_plaintext_path() -> None:
    with pytest.raises(ConfigError, match="no credential wrapping key"):
        get_secret_box(Settings(environment="development"))


def test_key_vault_is_preferred_when_configured() -> None:
    from ccreport.crypto import KeyVaultSecretBox

    box = get_secret_box(
        Settings(keyvault_url="https://vault.example/", dev_encryption_key=generate_dev_key())
    )
    assert isinstance(box, KeyVaultSecretBox)


def test_generated_development_key_is_32_bytes() -> None:
    assert len(base64.b64decode(generate_dev_key())) == 32


def test_short_key_is_rejected() -> None:
    with pytest.raises(ConfigError, match="16, 24 or 32 bytes"):
        DevSecretBox(b"too-short")
