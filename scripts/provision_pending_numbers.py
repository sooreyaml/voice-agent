#!/usr/bin/env python3
"""Provision phone numbers for organizations left pending by older releases.

Designed for a bounded deployment backfill. The command is idempotent: only
organizations without a business profile are selected, and the normal provisioning
service returns an existing live number if one already exists. Provider failures are
reported without blocking the application deployment.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.domains.telephony.provider import TwilioProvisioningService
from app.domains.telephony.service import provision_pending_organizations
from app.settings import settings
from app.store import Store

DEFAULT_LIMIT = 10


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"maximum organizations to process (default: {DEFAULT_LIMIT})",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    store = Store(settings.database_target)
    provider = TwilioProvisioningService(
        settings.twilio_account_sid,
        settings.twilio_auth_token,
        settings.openai_project_id,
    )
    try:
        result = provision_pending_organizations(
            store,
            provider,
            default_profile_template=settings.businesses_dir / "_default.yaml",
            default_timezone=settings.default_timezone,
            country=settings.number_pool_country,
            number_type=settings.number_pool_number_type,
            sms_enabled=settings.number_pool_sms_enabled,
            bundle_sid=settings.number_pool_bundle_sid or None,
            address_sid=settings.number_pool_address_sid or None,
            limit=max(0, args.limit),
        )
    finally:
        store.close()

    print(
        json.dumps(
            {
                "attempted": result.attempted,
                "provisioned": len(result.provisioned),
                "failed": len(result.failures),
                "failures": [
                    {"organization_id": organization_id, "code": code}
                    for organization_id, code in result.failures
                ],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
