#!/usr/bin/env python3
"""Provision the telephony side: a phone number and a SIP trunk that points at OpenAI.

    python scripts/twilio_setup.py search --country US --area-code 617
    python scripts/twilio_setup.py search --country GB --contains +4420
    python scripts/twilio_setup.py provision --country GB --contains +4420
    python scripts/twilio_setup.py show

Inbound calls then flow: caller -> Twilio number -> trunk -> OpenAI -> your webhook.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from app.domains.telephony.provider import (
    TelephonyProviderError,
    TwilioProvisioningService,
)

load_dotenv()


def provisioning_service() -> TwilioProvisioningService:
    return TwilioProvisioningService(
        os.environ.get("TWILIO_ACCOUNT_SID", ""),
        os.environ.get("TWILIO_AUTH_TOKEN", ""),
        os.environ.get("OPENAI_PROJECT_ID", ""),
    )


def describe(number) -> str:
    """Twilio leaves locality and region empty outside the US and Canada."""
    where = " ".join(p for p in (number["locality"], number["region"]) if p)
    requirement = number["address_requirements"]
    suffix = f" [{requirement} address required]" if requirement != "none" else ""
    return f"  {number['phone_number']}  {where}{suffix}".rstrip()


def cmd_search(args: argparse.Namespace) -> None:
    country = args.country.upper()
    numbers = provisioning_service().search_available_numbers(
        country,
        args.number_type,
        area_code=args.area_code,
        contains=args.contains,
        exclude_address_required=args.exclude_address_required,
        limit=args.limit,
    )
    if not numbers:
        hint = f" matching {args.contains}" if args.contains else ""
        print(f"No local voice numbers available in {country}{hint}.")
        return
    print(f"Available in {country}:")
    for number in numbers:
        print(describe(number))


def cmd_provision(args: argparse.Namespace) -> None:
    country = args.country.upper()
    provider = provisioning_service()
    phone_number = args.phone_number
    if phone_number is None:
        available = provider.search_available_numbers(
            country,
            args.number_type,
            area_code=args.area_code,
            contains=args.contains,
            limit=1,
        )
        if not available:
            hint = f" matching {args.contains}" if args.contains else ""
            sys.exit(
                f"No {args.number_type} voice numbers available in {country}{hint}."
            )
        phone_number = available[0]["phone_number"]

    result = provider.provision_number(
        phone_number,
        address_sid=args.address_sid,
        bundle_sid=args.bundle_sid,
        identity_sid=args.identity_sid,
        trunk_domain=args.domain,
    )
    print(f"Number ready: {result.phone_number} ({result.phone_number_sid})")
    print(f"Attached to shared trunk: {result.trunk_sid}")

    print(
        f"\nDone. Call {result.phone_number} once your server is running and the\n"
        "realtime.call.incoming webhook points at it."
    )


def cmd_show(args: argparse.Namespace) -> None:
    provider = provisioning_service()
    print(f"Expected origination URI: {provider.sip_uri}\n")
    for trunk in provider.trunk_configurations():
        print(f"Trunk {trunk.friendly_name} ({trunk.sid})")
        for sip_url, enabled in trunk.origination_urls:
            state = "enabled" if enabled else "disabled"
            print(f"  origination: {sip_url} [{state}]")
        for phone_number in trunk.phone_numbers:
            print(f"  number: {phone_number}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    contains_help = (
        "dialling prefix to match, e.g. +4420 for London. Use this instead of "
        "--area-code outside the US and Canada"
    )

    search = sub.add_parser("search", help="list numbers you could buy")
    search.add_argument(
        "--country", default="US", help="ISO country code, e.g. US or GB"
    )
    search.add_argument(
        "--number-type",
        choices=("local", "mobile", "national", "toll_free"),
        default="local",
    )
    search.add_argument(
        "--area-code", type=int, default=None, help="US and Canada only"
    )
    search.add_argument("--contains", default=None, help=contains_help)
    search.add_argument(
        "--exclude-address-required",
        action="store_true",
        help="exclude numbers that require a regulatory address",
    )
    search.add_argument("--limit", type=int, default=10)
    search.set_defaults(func=cmd_search)

    provision = sub.add_parser("provision", help="buy a number and wire up the trunk")
    provision.add_argument("--country", default="US")
    provision.add_argument(
        "--number-type",
        choices=("local", "mobile", "national", "toll_free"),
        default="local",
    )
    provision.add_argument(
        "--area-code", type=int, default=None, help="US and Canada only"
    )
    provision.add_argument("--contains", default=None, help=contains_help)
    provision.add_argument(
        "--phone-number",
        default=None,
        help="use this exact number instead of searching, buying it only if the "
        "account does not already own it",
    )
    provision.add_argument(
        "--domain",
        default=None,
        help="optional trunk domain, e.g. my-agent.pstn.twilio.com",
    )
    provision.add_argument("--address-sid", default=None)
    provision.add_argument("--bundle-sid", default=None)
    provision.add_argument("--identity-sid", default=None)
    provision.set_defaults(func=cmd_provision)

    show = sub.add_parser("show", help="print current trunk configuration")
    show.set_defaults(func=cmd_show)

    args = parser.parse_args()
    try:
        args.func(args)
    except (TelephonyProviderError, ValueError) as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
