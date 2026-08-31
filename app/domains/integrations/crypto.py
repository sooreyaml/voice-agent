"""At-rest encryption for provider credentials (roadmap section 11).

A single application key (``INTEGRATION_ENCRYPTION_KEY``, a urlsafe-base64 Fernet
key) wraps a JSON credential blob with AES-128-CBC + HMAC-SHA256. Outside
``development`` the key is mandatory; in development a fixed dev key is derived so
tests and local runs need no configuration.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.settings import Settings

from .constants import DEV_CIPHER_SEED
from .exceptions import IntegrationEncryptionNotConfigured


def _dev_key() -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(DEV_CIPHER_SEED).digest())


class CredentialCipher:
    """Seals/opens a small JSON object. Not for large payloads."""

    def __init__(self, key: str | bytes) -> None:
        material = key.encode() if isinstance(key, str) else key
        try:
            self._fernet = Fernet(material)
        except (ValueError, TypeError) as exc:
            raise IntegrationEncryptionNotConfigured(
                "INTEGRATION_ENCRYPTION_KEY is not a valid Fernet key "
                "(generate one with `python -c \"from cryptography.fernet import "
                'Fernet; print(Fernet.generate_key().decode())"`).'
            ) from exc

    def seal(self, data: dict[str, Any]) -> str:
        payload = json.dumps(data, separators=(",", ":"), sort_keys=True).encode()
        return self._fernet.encrypt(payload).decode()

    def open(self, token: str) -> dict[str, Any]:
        try:
            plaintext = self._fernet.decrypt(token.encode())
        except InvalidToken as exc:
            # The stored blob cannot be read with this key: a key rotation gone
            # wrong or a corrupted row, not something a caller can fix.
            raise RuntimeError(
                "stored integration credentials could not be decrypted"
            ) from exc
        decoded = json.loads(plaintext.decode())
        if isinstance(decoded, dict):
            return decoded
        raise RuntimeError("decrypted integration credentials are not an object")


def build_cipher(settings: Settings) -> CredentialCipher:
    key = settings.integration_encryption_key
    if key:
        return CredentialCipher(key)
    if settings.environment == "development":
        return CredentialCipher(_dev_key())
    raise IntegrationEncryptionNotConfigured(
        "INTEGRATION_ENCRYPTION_KEY must be set outside development."
    )
