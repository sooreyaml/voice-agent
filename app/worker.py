"""Background worker: drains the outbound webhook delivery queue.

Run as its own process alongside the API:

    python -m app.worker

It shares the same database (``DATABASE_URL`` / ``DATABASE_PATH``). Multiple
workers are safe against Postgres (claiming uses ``FOR UPDATE SKIP LOCKED``);
with SQLite run exactly one. New periodic jobs (billing meter flush, provider
reconciliation) can be added as extra tickers in ``run``.
"""

from __future__ import annotations

import asyncio
import logging
import signal

import httpx

from .domains.billing.provider import StripeBillingService
from .domains.billing.services import lifecycle as billing_lifecycle
from .domains.billing.services import management as billing_service
from .domains.integrations import crm_sync
from .domains.integrations.crypto import CredentialCipher, build_cipher
from .domains.privacy import service as privacy_service
from .domains.telephony.pool import refill_pool
from .domains.telephony.provider import TwilioProvisioningService
from .domains.webhooks import service as webhook_service
from .settings import settings
from .store import Store

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
logger = logging.getLogger("callagent.worker")

IDLE_SLEEP_SECONDS = 2.0
BUSY_SLEEP_SECONDS = 0.1
COMPLIANCE_SLEEP_SECONDS = 30.0
LIFECYCLE_SLEEP_SECONDS = 60.0
POOL_SLEEP_SECONDS = 300.0
USAGE_EXPORT_SLEEP_SECONDS = 120.0
USAGE_EXPORT_BATCH = 200
# Cap how many numbers one refill pass will buy, so a misconfigured target or a
# Twilio hiccup can't run up a bill in a tight loop.
POOL_REFILL_MAX_PER_PASS = 5


async def _webhook_ticker(store: Store, stop: asyncio.Event) -> None:
    limits = httpx.Limits(max_connections=20)
    async with httpx.AsyncClient(
        limits=limits, timeout=settings.webhook_timeout_seconds, follow_redirects=False
    ) as client:
        while not stop.is_set():
            try:
                processed = await webhook_service.process_due_deliveries(
                    store, client, timeout=settings.webhook_timeout_seconds
                )
            except Exception:
                logger.exception("webhook delivery pass failed")
                processed = 0
            delay = BUSY_SLEEP_SECONDS if processed else IDLE_SLEEP_SECONDS
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass


async def _crm_ticker(
    store: Store, cipher: CredentialCipher, stop: asyncio.Event
) -> None:
    while not stop.is_set():
        try:
            processed = await asyncio.to_thread(
                crm_sync.process_due_crm_jobs, store, cipher
            )
        except Exception:
            logger.exception("crm sync pass failed")
            processed = 0
        delay = BUSY_SLEEP_SECONDS if processed else IDLE_SLEEP_SECONDS
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            pass


async def _privacy_ticker(store: Store, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            purged, requests = await asyncio.to_thread(
                lambda: (
                    privacy_service.purge_expired_transcripts(store),
                    privacy_service.process_due_data_requests(store),
                )
            )
            if purged or requests:
                logger.info(
                    "privacy pass purged %d transcript(s), processed %d request(s)",
                    purged,
                    requests,
                )
        except Exception:
            logger.exception("privacy and data-rights pass failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=COMPLIANCE_SLEEP_SECONDS)
        except TimeoutError:
            pass


async def _lifecycle_ticker(store: Store, stop: asyncio.Event) -> None:
    """Reap never-paid signups and suspend past-due tenants."""
    while not stop.is_set():
        try:
            reaped, suspended = await asyncio.to_thread(
                lambda: (
                    billing_lifecycle.reap_abandoned_signups(
                        store,
                        grace_hours=settings.signup_checkout_grace_hours,
                        quarantine_days=settings.number_quarantine_days,
                    ),
                    billing_lifecycle.suspend_overdue(
                        store, grace_days=settings.dunning_grace_days
                    ),
                )
            )
            if reaped or suspended:
                logger.info(
                    "lifecycle pass: %d reaped, %d suspended", reaped, suspended
                )
        except Exception:
            logger.exception("organization lifecycle pass failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=LIFECYCLE_SLEEP_SECONDS)
        except TimeoutError:
            pass


async def _pool_ticker(
    store: Store, provider: TwilioProvisioningService, stop: asyncio.Event
) -> None:
    """Keep the pre-warmed number pool topped up and recycle quarantined numbers."""
    while not stop.is_set():
        try:
            promoted = await asyncio.to_thread(
                store.promote_quarantined_pool_numbers
            )
            result = await asyncio.to_thread(
                refill_pool,
                store,
                provider,
                country=settings.number_pool_country,
                target=settings.number_pool_target,
                number_type=settings.number_pool_number_type,
                sms_enabled=settings.number_pool_sms_enabled,
                bundle_sid=settings.number_pool_bundle_sid or None,
                address_sid=settings.number_pool_address_sid or None,
                max_buy=POOL_REFILL_MAX_PER_PASS,
            )
            if promoted or result.bought or result.errors:
                logger.info(
                    "pool pass: promoted %d, %s", promoted, result.short
                )
        except Exception:
            logger.exception("number pool refill pass failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=POOL_SLEEP_SECONDS)
        except TimeoutError:
            pass


async def _usage_export_ticker(
    store: Store, provider: StripeBillingService, stop: asyncio.Event
) -> None:
    """Push metered call usage to the Stripe meter (was an admin-poked route)."""
    while not stop.is_set():
        try:
            result = await asyncio.to_thread(
                billing_service.export_usage,
                store,
                provider,
                limit=USAGE_EXPORT_BATCH,
            )
            if result.get("sent") or result.get("failed"):
                logger.info(
                    "usage export: %d sent, %d failed",
                    result.get("sent", 0),
                    result.get("failed", 0),
                )
        except Exception:
            logger.exception("usage export pass failed")
        try:
            await asyncio.wait_for(
                stop.wait(), timeout=USAGE_EXPORT_SLEEP_SECONDS
            )
        except TimeoutError:
            pass


async def run() -> None:
    # The API process is the single migration owner. Compose starts both
    # services together, so allowing the worker to migrate as well races two
    # Alembic upgrades against the same database on a fresh deployment.
    store = await asyncio.to_thread(Store, settings.database_target, migrate=False)
    cipher = build_cipher(settings)
    logger.info("worker started (%s backend)", store.dialect)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    tickers = [
        _webhook_ticker(store, stop),
        _crm_ticker(store, cipher, stop),
        _privacy_ticker(store, stop),
    ]
    # The reaper / dunning sweep only has anything to do when billing drives the
    # subscription lifecycle.
    if settings.billing_enabled:
        tickers.append(_lifecycle_ticker(store, stop))
    if settings.number_pool_refill_enabled:
        twilio = TwilioProvisioningService(
            settings.twilio_account_sid,
            settings.twilio_auth_token,
            settings.openai_project_id,
        )
        tickers.append(_pool_ticker(store, twilio, stop))
    else:
        logger.info(
            "number pool refill disabled "
            "(NUMBER_POOL_AUTO_REFILL_ENABLED=false or NUMBER_POOL_TARGET=0)"
        )

    if settings.billing_enabled and settings.stripe_secret_key:
        stripe = StripeBillingService(
            settings.stripe_secret_key, settings.stripe_webhook_secret
        )
        tickers.append(_usage_export_ticker(store, stripe, stop))

    try:
        await asyncio.gather(*tickers)
    finally:
        await asyncio.to_thread(store.close)
        logger.info("worker stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
