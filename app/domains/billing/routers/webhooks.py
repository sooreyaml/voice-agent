from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.domains.auth.dependencies import StoreDep

from ..dependencies import StripeBillingDep
from ..provider import SignatureVerificationError, StripeBillingError
from ..services.webhooks import process_webhook

router = APIRouter(tags=["billing webhooks"])


@router.post(
    "/webhooks/stripe",
    summary="Receive signed Stripe subscription and invoice events",
    responses={400: {"description": "Invalid payload or Stripe signature"}},
)
async def stripe_webhook(
    request: Request,
    store: StoreDep,
    provider: StripeBillingDep,
) -> dict[str, object]:
    payload = await request.body()
    signature = request.headers.get("stripe-signature")
    try:
        return await run_in_threadpool(
            process_webhook,
            store,
            provider,
            payload,
            signature,
        )
    except (SignatureVerificationError, StripeBillingError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail="invalid Stripe webhook") from exc
