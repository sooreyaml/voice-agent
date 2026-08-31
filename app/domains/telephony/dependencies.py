from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.domains.auth.dependencies import SettingsDep
from app.domains.businesses.repository import BusinessRepository

from .provider import TwilioProvisioningService


def get_twilio_provisioning_service(
    settings: SettingsDep,
) -> TwilioProvisioningService:
    return TwilioProvisioningService(
        settings.twilio_account_sid,
        settings.twilio_auth_token,
        settings.openai_project_id,
    )


TwilioProvisioningDep = Annotated[
    TwilioProvisioningService, Depends(get_twilio_provisioning_service)
]


def get_business_repository(request: Request) -> BusinessRepository:
    return request.app.state.business_repository


BusinessRepositoryDep = Annotated[BusinessRepository, Depends(get_business_repository)]
