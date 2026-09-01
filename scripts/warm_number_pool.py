#!/usr/bin/env python3
"""Buy phone numbers into the pre-warmed pool that signup hands out.

    python scripts/warm_number_pool.py --count 10        # buy 10 more now
    python scripts/warm_number_pool.py --target 20       # top up to 20 available
    python scripts/warm_number_pool.py --status          # show pool counts

Numbers are bought on the platform Twilio account and attached to the shared
OpenAI SIP trunk, exactly like the runtime pool-refill job. Set
TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, OPENAI_PROJECT_ID and DATABASE_URL first.
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
    parser.add_argument("--country", help="ISO country (default: NUMBER_POOL_COUNTRY)")
    parser.add_argument("--number-type", default="local")
    return parser


def main() -> None:
    args = _parser().parse_args()
    settings = load_settings()
    store = Store(settings.database_target)

    if args.status:
        counts = store.pool_counts()
        print("number pool:")
        for status in ("available", "assigned", "quarantined", "retired"):
            print(f"  {status:12} {counts.get(status, 0)}")
        return

    provider = TwilioProvisioningService(
        settings.twilio_account_sid,
        settings.twilio_auth_token,
        settings.openai_project_id,
    )
    country = (args.country or settings.number_pool_country).upper()

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
        number_type=args.number_type,
        max_buy=max_buy,
    )
    print(f"pool refill: {result.short}")
    for e164, code in result.errors:
        print(f"  failed {e164}: {code}")
    if result.errors and result.bought == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
