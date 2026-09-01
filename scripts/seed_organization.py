"""Seed a complete organization from the canonical business profile template.

The command is idempotent. It creates or reconciles the organization, a verified
owner account, owner membership, active onboarding record, and published profile.
Set DATABASE_URL and SEED_OWNER_PASSWORD in the environment before running it.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.domains.organizations.seeding import OrganizationSeed, seed_organization
from app.store import Store

DEFAULT_TEMPLATE = ROOT / "businesses" / "harborview-dental.yaml"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--organization-name", required=True)
    parser.add_argument("--organization-slug", required=True)
    parser.add_argument("--owner-email", required=True)
    parser.add_argument("--business-name", required=True)
    parser.add_argument(
        "--business-slug",
        help="defaults to --organization-slug",
    )
    parser.add_argument(
        "--phone-number",
        action="append",
        required=True,
        dest="phone_numbers",
        help="repeat for every inbound E.164 number",
    )
    parser.add_argument("--timezone", default="Europe/London")
    parser.add_argument("--agent-name", default="Alex")
    parser.add_argument("--business-description")
    parser.add_argument("--greeting")
    parser.add_argument("--contact-email")
    parser.add_argument("--contact-phone")
    parser.add_argument("--website")
    parser.add_argument("--transfer-number")
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    return parser


def main() -> None:
    args = _parser().parse_args()
    database_target = (
        os.environ.get("DATABASE_URL", "").strip()
        or os.environ.get("DATABASE_PATH", "").strip()
    )
    if not database_target:
        raise SystemExit("DATABASE_URL (or DATABASE_PATH for local use) is required")
    owner_password = os.environ.get("SEED_OWNER_PASSWORD", "")
    if not owner_password and sys.stdin.isatty():
        owner_password = getpass.getpass("Initial owner password: ")
    if not owner_password:
        raise SystemExit("SEED_OWNER_PASSWORD is required")

    template_path = args.template
    if not template_path.is_absolute():
        template_path = ROOT / template_path

    seed = OrganizationSeed(
        organization_name=args.organization_name,
        organization_slug=args.organization_slug,
        owner_email=args.owner_email,
        business_name=args.business_name,
        business_slug=args.business_slug or args.organization_slug,
        phone_numbers=tuple(args.phone_numbers),
        timezone=args.timezone,
        agent_name=args.agent_name,
        business_description=args.business_description,
        greeting=args.greeting,
        contact_email=args.contact_email,
        contact_phone=args.contact_phone,
        website=args.website,
        transfer_number=args.transfer_number,
    )

    store = Store(database_target)
    try:
        result = seed_organization(
            store,
            template_path=template_path,
            seed=seed,
            owner_password=owner_password,
        )
    finally:
        store.close()

    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
