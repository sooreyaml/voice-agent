from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from app.domains.auth.dependencies import SettingsDep
from app.domains.auth.exceptions import NotFound

from .provider import StripeBillingService


def require_billing_enabled(settings: SettingsDep) -> None:
    """Every billing route is registered, but only reachable when
    ``BILLING_ENABLED`` is on. Off by default right now.
    """
    if not settings.billing_enabled:
        raise NotFound("Billing is not enabled on this deployment.")


BillingEnabledDep = Depends(require_billing_enabled)


def get_stripe_billing_service(settings: SettingsDep) -> StripeBillingService:
    return StripeBillingService(
        settings.stripe_secret_key,
        settings.stripe_webhook_secret,
    )


StripeBillingDep = Annotated[
    StripeBillingService, Depends(get_stripe_billing_service)
]
