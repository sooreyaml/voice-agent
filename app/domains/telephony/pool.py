"""Optionally keep a stock of ready-to-hand-out phone numbers.

Signup purchases on demand when no recycled number is available. Deployments
that prefer to pre-warm can opt in with ``NUMBER_POOL_TARGET``. Called from
``scripts/warm_number_pool.py`` and the pool-refill ticker in ``app/worker.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.store import Store

from .provider import TelephonyProviderError, TwilioProvisioningService

logger = logging.getLogger(__name__)


@dataclass
class RefillResult:
    needed: int
    bought: int
    available: int
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def short(self) -> str:
        note = f"bought {self.bought}/{self.needed}, {self.available} available"
        if self.errors:
            note += f", {len(self.errors)} failed"
        return note


def refill_pool(
    store: Store,
    provider: TwilioProvisioningService,
    *,
    country: str,
    target: int,
    number_type: str = "local",
    sms_enabled: bool = False,
    bundle_sid: str | None = None,
    address_sid: str | None = None,
    max_buy: int | None = None,
) -> RefillResult:
    """Top the ``available`` pool count up towards ``target``.

    Buys one number at a time and attaches it to the shared trunk; a provider
    error on one candidate is recorded and the next is tried. ``max_buy`` caps a
    single run (the worker passes a small cap; the CLI leaves it open).

    ``sms_enabled`` restricts the search to numbers that can also send/receive
    SMS (GB mobile numbers generally can; some geographic numbers cannot).
    ``bundle_sid``/``address_sid`` are the Twilio regulatory bundle and address
    a country such as GB requires before it will sell a number. When a bundle is
    configured we stop filtering out address-required numbers, since every GB
    geographic number needs one and we can now satisfy it.
    """
    country = country.upper()
    have = store.available_pool_count()
    needed = max(target - have, 0)
    if max_buy is not None:
        needed = min(needed, max_buy)
    if needed <= 0:
        return RefillResult(needed=0, bought=0, available=have)

    try:
        candidates = provider.search_available_numbers(
            country,
            number_type,
            exclude_address_required=not bundle_sid,
            sms_enabled=sms_enabled,
            limit=needed + 5,
        )
    except (TelephonyProviderError, ValueError) as exc:
        logger.warning("number pool refill search failed: %s", exc)
        return RefillResult(
            needed=needed, bought=0, available=have, errors=[("search", str(exc))]
        )

    bought = 0
    errors: list[tuple[str, str]] = []
    for candidate in candidates:
        if bought >= needed:
            break
        e164 = str(candidate["phone_number"])
        try:
            result = provider.provision_number(
                e164, bundle_sid=bundle_sid, address_sid=address_sid
            )
        except TelephonyProviderError as exc:
            logger.warning("could not buy %s for the pool: %s", e164, exc.code)
            errors.append((e164, exc.code))
            continue
        inserted = store.add_pool_number(
            e164,
            country,
            provider_number_sid=result.phone_number_sid,
            provider_trunk_sid=result.trunk_sid,
        )
        if inserted:
            bought += 1
            logger.info("added %s to the number pool", e164)

    return RefillResult(
        needed=needed,
        bought=bought,
        available=store.available_pool_count(),
        errors=errors,
    )
