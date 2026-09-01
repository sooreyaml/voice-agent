#!/usr/bin/env python3
"""Buy phone numbers into the pre-warmed pool that signup hands out.

    python scripts/warm_number_pool.py --count 10        # buy 10 more now
    python scripts/warm_number_pool.py --target 20       # top up to 20 available
    python scripts/warm_number_pool.py --status          # show pool counts
    python scripts/warm_number_pool.py --retire-foreign  # retire numbers not in
                                                         # NUMBER_POOL_COUNTRY

Numbers are bought on the platform Twilio account and attached to the shared
OpenAI SIP trunk, exactly like the runtime pool-refill job. Set
TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, OPENAI_PROJECT_ID and DATABASE_URL first.

For a country such as GB, Twilio needs an approved regulatory bundle and address
before it will sell a number. Set NUMBER_POOL_BUNDLE_SID and
NUMBER_POOL_ADDRESS_SID (or pass --bundle-sid / --address-sid).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from app.domains.telephony.pool import refill_pool
from app.domains.telephony.provider import TwilioProvisioningService
from app.settings import load_settings
from app.store import Store

load_dotenv()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--count", type=int, help="buy exactly this many more numbers now"
    )
    group.add_argument(
        "--target", type=int, help="buy until this many numbers are available"
    )
    group.add_argument(
        "--status", action="store_true", help="print pool counts and exit"
    )
    group.add_argument(
        "--retire-foreign",
        action="store_true",
        help="retire every available number whose country is not "
        "NUMBER_POOL_COUNTRY (use after changing the pool country)",
    )
    parser.add_argument("--country", help="ISO country (default: NUMBER_POOL_COUNTRY)")
    parser.add_argument(
        "--number-type", help="local | mobile | national | tollFree (default: setting)"
    )
    parser.add_argument(
        "--no-sms",
        action="store_true",
        help="do not require SMS-capable numbers (default: require, per "
        "NUMBER_POOL_SMS_ENABLED)",
    )
    parser.add_argument("--bundle-sid", help="Twilio regulatory bundle SID override")
    parser.add_argument("--address-sid", help="Twilio address SID override")
    return parser


def main() -> None:
    args = _parser().parse_args()
    settings = load_settings()
    store = Store(settings.database_target)
    country = (args.country or settings.number_pool_country).upper()

    if args.status:
        counts = store.pool_counts()
        print("number pool:")
        for status in ("available", "assigned", "quarantined", "retired"):
            print(f"  {status:12} {counts.get(status, 0)}")
        return

    if args.retire_foreign:
        retired = store.retire_available_pool_numbers(exclude_country=country)
        print(f"retired {len(retired)} number(s) not in {country}:")
        for row in retired:
            print(f"  {row['e164']:16} {row.get('provider_number_sid') or '-'}")
        if retired:
            print(
                "\nThese are still owned on Twilio. Release them in the console"
                " (Phone Numbers > Manage > Active numbers) to stop being billed."
            )
        return

    provider = TwilioProvisioningService(
        settings.twilio_account_sid,
        settings.twilio_auth_token,
        settings.openai_project_id,
    )
    number_type = args.number_type or settings.number_pool_number_type
    sms_enabled = settings.number_pool_sms_enabled and not args.no_sms
    bundle_sid = args.bundle_sid or settings.number_pool_bundle_sid or None
    address_sid = args.address_sid or settings.number_pool_address_sid or None

    if args.count is not None:
        available = store.available_pool_count()
        target = available + max(args.count, 0)
        max_buy = max(args.count, 0)
    else:
        target = max(args.target, 0)
        max_buy = None

    result = refill_pool(
        store,
        provider,
        country=country,
        target=target,
        number_type=number_type,
        sms_enabled=sms_enabled,
        bundle_sid=bundle_sid,
        address_sid=address_sid,
        max_buy=max_buy,
    )
    print(f"pool refill: {result.short}")
    for e164, code in result.errors:
        print(f"  failed {e164}: {code}")
    if result.errors and result.bought == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
