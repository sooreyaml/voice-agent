from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from app.domains.auth.dependencies import SettingsDep

from .crypto import CredentialCipher, build_cipher


def get_credential_cipher(settings: SettingsDep) -> CredentialCipher:
    return build_cipher(settings)


CredentialCipherDep = Annotated[CredentialCipher, Depends(get_credential_cipher)]
