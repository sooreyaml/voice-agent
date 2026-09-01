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

from .domains.integrations import crm_sync
from .domains.integrations.crypto import CredentialCipher, build_cipher
from .domains.privacy import service as privacy_service
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
    try:
        await asyncio.gather(
            _webhook_ticker(store, stop),
            _crm_ticker(store, cipher, stop),
            _privacy_ticker(store, stop),
        )
    finally:
        await asyncio.to_thread(store.close)
        logger.info("worker stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
