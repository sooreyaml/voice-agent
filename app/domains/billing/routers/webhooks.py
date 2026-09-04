from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.domains.auth.dependencies import SettingsDep, StoreDep
from app.domains.onboarding import service as onboarding_service
from app.domains.telephony.dependencies import ProvisioningProviderDep

from ..dependencies import BillingEnabledDep, StripeBillingDep
from ..provider import SignatureVerificationError, StripeBillingError
from ..services.webhooks import process_webhook

logger = logging.getLogger("callagent.billing")

router = APIRouter(tags=["billing webhooks"], dependencies=[BillingEnabledDep])


@router.post(
    "/webhooks/stripe",
    summary="Receive signed Stripe subscription and invoice events",
    responses={400: {"description": "Invalid payload or Stripe signature"}},
)
async def stripe_webhook(
    request: Request,
    store: StoreDep,
    settings: SettingsDep,
    provider: StripeBillingDep,
    provisioning_provider: ProvisioningProviderDep,
) -> dict[str, object]:
    payload = await request.body()
    signature = request.headers.get("stripe-signature")
    try:
        result = await run_in_threadpool(
            process_webhook,
            store,
            provider,
            payload,
            signature,
        )
    except (SignatureVerificationError, StripeBillingError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail="invalid Stripe webhook") from exc

    organization_id = result.get("organization_id")
    if result.get("outcome") == "processed" and organization_id:
        # A completed checkout is the moment a gated signup earns its number.
        # Best-effort: the provisioning sweep is the backstop if this misses.
        try:
            await run_in_threadpool(
                onboarding_service.provision_after_payment,
                store,
                provisioning_provider,
                settings,
                str(organization_id),
            )
        except Exception:
            # A provisioning error must never turn a good webhook into a 500 —
            # Stripe would retry it forever. The sweep will pick the org up.
            logger.exception(
                "post-payment provisioning raised for %s", organization_id
            )
    return result
