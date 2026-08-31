from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from app.domains.auth.dependencies import SettingsDep

from .provider import StripeBillingService


def get_stripe_billing_service(settings: SettingsDep) -> StripeBillingService:
    return StripeBillingService(
        settings.stripe_secret_key,
        settings.stripe_webhook_secret,
    )


StripeBillingDep = Annotated[
    StripeBillingService, Depends(get_stripe_billing_service)
]
