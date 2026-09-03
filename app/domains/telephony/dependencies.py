from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from .provider import TwilioProvisioningService


def get_provisioning_provider(request: Request) -> TwilioProvisioningService:
    settings = request.app.state.settings
    return TwilioProvisioningService(
        settings.twilio_account_sid,
        settings.twilio_auth_token,
        settings.openai_project_id,
    )


ProvisioningProviderDep = Annotated[
    TwilioProvisioningService, Depends(get_provisioning_provider)
]
