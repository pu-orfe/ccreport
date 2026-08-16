"""Envelope encryption for stored mailbox credentials.

A refresh token or an IMAP app password is a long-lived bearer secret: whoever
holds it can read somebody's mail until they notice and revoke it. So neither is
ever written in the clear, and the key that protects them does not live in the
same database they do.

The scheme is ordinary envelope encryption. A fresh 256-bit data key encrypts the
secret with AES-GCM; the data key itself is wrapped by a key encryption key and
stored beside the ciphertext. Rotating the KEK is then a re-wrap of small
blobs rather than a forced reconnect for every faculty member — which matters,
because a forced reconnect is exactly the event that makes people abandon a tool.

Two implementations:

* :class:`KeyVaultSecretBox` wraps with an RSA key in Azure Key Vault. The
  private key never leaves the vault; unwrapping is a call, not a download.
* :class:`DevSecretBox` wraps with a local symmetric key from settings. It
  refuses to construct on Azure or in production, because a KEK sitting in an
  environment variable next to its ciphertext is not a KEK.
"""

from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .errors import ConfigError
from .settings import Settings, get_settings

#: AES-GCM nonces are 96 bits; reusing one with the same key is catastrophic, so
#: every seal generates a fresh random nonce and never derives one.
NONCE_BYTES = 12
DEK_BYTES = 32
DEV_KEY_NAME = "dev-local"


@dataclass(frozen=True, slots=True)
class SealedSecret:
    """Ciphertext plus everything needed to open it again."""

    wrapped_dek: bytes
    key_name: str
    key_version: str | None
    nonce: bytes
    ciphertext: bytes


@runtime_checkable
class SecretBox(Protocol):
    """Seals and opens small secrets. No key material crosses this boundary."""

    key_name: str

    def seal(self, plaintext: bytes) -> SealedSecret: ...

    def open(self, sealed: SealedSecret) -> bytes: ...


def _aesgcm(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover - dependency is in [azure]
        raise ConfigError(
            "the 'cryptography' package is required to store mailbox credentials; "
            "install ccreport[azure] or ccreport[app]"
        ) from exc
    return AESGCM(key)


def _encrypt(dek: bytes, plaintext: bytes, *, aad: bytes | None = None) -> tuple[bytes, bytes]:
    nonce = os.urandom(NONCE_BYTES)
    return nonce, _aesgcm(dek).encrypt(nonce, plaintext, aad)


def _decrypt(dek: bytes, nonce: bytes, ciphertext: bytes, *, aad: bytes | None = None) -> bytes:
    return _aesgcm(dek).decrypt(nonce, ciphertext, aad)


class DevSecretBox:
    """Local symmetric wrapping, for development and tests only.

    The refusal below is the point of the class. A development shortcut that
    survives to production is not a shortcut, it is the production key
    management story, and nobody would have chosen it deliberately.
    """

    def __init__(self, key: bytes, *, key_name: str = DEV_KEY_NAME):
        if len(key) not in (16, 24, 32):
            raise ConfigError(
                f"development encryption key must be 16, 24 or 32 bytes, got {len(key)}"
            )
        self._kek = key
        self.key_name = key_name

    @classmethod
    def from_settings(cls, settings: Settings) -> DevSecretBox:
        if settings.on_azure or settings.is_production:
            raise ConfigError(
                "the development secret box refuses to run on Azure or in "
                "production. Set CCREPORT_KEYVAULT_URL so mailbox credentials "
                "are wrapped by a key that does not live beside them."
            )
        if not settings.dev_encryption_key:
            raise ConfigError(
                "no credential wrapping key is configured. Set "
                "CCREPORT_KEYVAULT_URL for a deployed environment, or "
                "CCREPORT_DEV_ENCRYPTION_KEY (base64, 32 bytes) locally. "
                "`ccreport doctor` prints a ready-made key."
            )
        raw = settings.dev_encryption_key.get_secret_value().strip()
        try:
            key = base64.b64decode(raw, validate=True)
        except (ValueError, TypeError) as exc:
            raise ConfigError("CCREPORT_DEV_ENCRYPTION_KEY must be base64") from exc
        return cls(key)

    def seal(self, plaintext: bytes) -> SealedSecret:
        dek = secrets.token_bytes(DEK_BYTES)
        nonce, ciphertext = _encrypt(dek, plaintext)
        wrap_nonce, wrapped = _encrypt(self._kek, dek)
        return SealedSecret(
            wrapped_dek=wrap_nonce + wrapped,
            key_name=self.key_name,
            key_version=None,
            nonce=nonce,
            ciphertext=ciphertext,
        )

    def open(self, sealed: SealedSecret) -> bytes:
        wrap_nonce, wrapped = sealed.wrapped_dek[:NONCE_BYTES], sealed.wrapped_dek[NONCE_BYTES:]
        dek = _decrypt(self._kek, wrap_nonce, wrapped)
        return _decrypt(dek, sealed.nonce, sealed.ciphertext)


class KeyVaultSecretBox:
    """RSA-OAEP key wrapping against an Azure Key Vault key.

    ``key_version`` is recorded per record rather than assumed global, so a
    rotation can proceed record by record while the application keeps serving.
    """

    ALGORITHM = "RSA-OAEP-256"

    def __init__(self, vault_url: str, key_name: str, *, credential=None):
        self.vault_url = vault_url.rstrip("/")
        self.key_name = key_name
        self._credential = credential
        self._client = None

    @classmethod
    def from_settings(cls, settings: Settings) -> KeyVaultSecretBox:
        if not settings.keyvault_url:
            raise ConfigError("CCREPORT_KEYVAULT_URL is not set")
        return cls(settings.keyvault_url, settings.keyvault_key_name)

    def _crypto_client(self):
        if self._client is None:
            try:
                from azure.identity import DefaultAzureCredential
                from azure.keyvault.keys import KeyClient
                from azure.keyvault.keys.crypto import CryptographyClient
            except ImportError as exc:  # pragma: no cover - dependency is in [azure]
                raise ConfigError(
                    "Key Vault wrapping needs the azure extras; install ccreport[azure]"
                ) from exc
            credential = self._credential or DefaultAzureCredential()
            key = KeyClient(vault_url=self.vault_url, credential=credential).get_key(self.key_name)
            self._client = CryptographyClient(key, credential=credential)
        return self._client

    @property
    def key_version(self) -> str | None:
        client = self._crypto_client()
        key_id = getattr(client, "key_id", "") or ""
        return key_id.rstrip("/").rsplit("/", 1)[-1] or None

    def seal(self, plaintext: bytes) -> SealedSecret:
        dek = secrets.token_bytes(DEK_BYTES)
        nonce, ciphertext = _encrypt(dek, plaintext)
        result = self._crypto_client().wrap_key(self.ALGORITHM, dek)
        return SealedSecret(
            wrapped_dek=result.encrypted_key,
            key_name=self.key_name,
            key_version=self.key_version,
            nonce=nonce,
            ciphertext=ciphertext,
        )

    def open(self, sealed: SealedSecret) -> bytes:
        dek = self._crypto_client().unwrap_key(self.ALGORITHM, sealed.wrapped_dek).key
        return _decrypt(dek, sealed.nonce, sealed.ciphertext)


def get_secret_box(settings: Settings | None = None) -> SecretBox:
    """Key Vault when configured, the development box when explicitly allowed."""
    settings = settings or get_settings()
    if settings.keyvault_url:
        return KeyVaultSecretBox.from_settings(settings)
    return DevSecretBox.from_settings(settings)


def generate_dev_key() -> str:
    """A base64 32-byte key, ready to paste into ``CCREPORT_DEV_ENCRYPTION_KEY``."""
    return base64.b64encode(secrets.token_bytes(DEK_BYTES)).decode("ascii")


__all__ = [
    "DEV_KEY_NAME",
    "DevSecretBox",
    "KeyVaultSecretBox",
    "SealedSecret",
    "SecretBox",
    "generate_dev_key",
    "get_secret_box",
]
