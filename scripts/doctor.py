#!/usr/bin/env python3
"""Preflight check: verifies credentials and wiring before you place a real call.

    python scripts/doctor.py

Reports PASS / WARN / FAIL for each step. Secrets are never printed, only
whether they are present and whether they work.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import websockets
import yaml
from dotenv import load_dotenv
from websockets.asyncio.client import connect

from app.business import BusinessProfile
from app.domains.businesses.schemas import PublishedBusinessConfiguration
from app.settings import load_settings

load_dotenv()

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
ICON = {PASS: "\033[32m✓\033[0m", WARN: "\033[33m!\033[0m", FAIL: "\033[31m✗\033[0m"}

results: list[tuple[str, str]] = []


def report(status: str, message: str, hint: str = "") -> None:
    results.append((status, message))
    print(f" {ICON[status]} {message}")
    if hint and status != PASS:
        print(f"     → {hint}")


def digits(value: str) -> str:
    return "".join(c for c in value if c.isdigit())


def error_message(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", {}).get("message", ""))[:160]
    except ValueError:
        return response.text[:160]


PROBE_TIMEOUT_SECONDS = 45


async def _ask_model_to_speak(settings) -> tuple[bool, str]:
    url = f"https://api.openai.com/v1/realtime?model={settings.realtime_model}"
    try:
        socket = await connect(
            url.replace("https://", "wss://", 1),
            additional_headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            open_timeout=20,
        )
    except websockets.exceptions.InvalidStatus as exc:
        return False, f"the API refused a {settings.realtime_model} session ({exc})"
    except Exception as exc:  # noqa: BLE001 - report any failed preflight condition
        return False, str(exc)

    async with socket as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "instructions": "Reply with the single word: ready",
                        "output_modalities": ["text"],
                    },
                }
            )
        )
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                while True:
                    event = json.loads(await ws.recv())
                    if event.get("type") == "error":
                        error = event.get("error", {})
                        return False, str(error.get("message") or error)[:200]
                    if event.get("type") == "response.done":
                        status = event.get("response", {}).get("status")
                        if status == "completed":
                            return True, ""
                        details = event.get("response", {}).get("status_details", {})
                        return False, f"response {status}: {json.dumps(details)[:150]}"
        except TimeoutError:
            return False, f"no response within {PROBE_TIMEOUT_SECONDS}s"


def probe_realtime(settings) -> tuple[bool, str]:
    """Make the realtime model actually produce something.

    Every cheaper check lies. A project with no realtime access is still handed a
    200 by /v1/realtime/client_secrets and can still open the WebSocket, and
    /v1/realtime/calls refuses even projects that can take calls happily. Only
    completing a real turn distinguishes "configured" from "will answer the
    phone", which is the difference between this passing and callers hearing
    ringing forever. One short text turn costs a fraction of a penny.
    """
    try:
        return asyncio.run(_ask_model_to_speak(settings))
    except Exception as exc:  # noqa: BLE001 - report any failed preflight condition
        return False, str(exc)


def check_openai(settings) -> None:
    print("\nOpenAI")
    if not settings.openai_api_key:
        report(FAIL, "OPENAI_API_KEY missing", "Add it to .env")
        return

    try:
        response = httpx.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        report(FAIL, f"could not reach the OpenAI API ({exc})")
        return

    if response.status_code == 401:
        report(FAIL, "OPENAI_API_KEY was rejected", "Check the key is current")
        return
    if response.status_code >= 400:
        report(
            FAIL, f"OpenAI API returned {response.status_code}: {response.text[:120]}"
        )
        return
    report(PASS, "API key works")

    allowed, detail = probe_realtime(settings)
    if allowed:
        report(PASS, f"{settings.realtime_model} completed a live turn")
    else:
        report(
            FAIL,
            f"{settings.realtime_model} could not complete a turn",
            f"{detail}\n     → Callers will hear ringing and nothing else. Allow the "
            "realtime models for this project at "
            "platform.openai.com/settings > Project > Limits.",
        )

    # Text models, unlike realtime ones, are reported accurately here.
    summary = httpx.get(
        f"https://api.openai.com/v1/models/{settings.summary_model}",
        headers={"Authorization": f"Bearer {settings.openai_api_key}"},
        timeout=15.0,
    )
    if summary.status_code < 300:
        report(PASS, f"summary model available: {settings.summary_model}")
    else:
        report(
            WARN,
            f"summary model {settings.summary_model} is not available to this project",
            "Calls still work; only the post-call summary would fail. "
            "Set SUMMARY_MODEL to a text model the project can use.",
        )

    if not settings.openai_project_id:
        report(FAIL, "OPENAI_PROJECT_ID missing", "Settings > Project > General")
    elif not settings.openai_project_id.startswith("proj_"):
        report(FAIL, f"OPENAI_PROJECT_ID looks wrong: {settings.openai_project_id}")
    else:
        report(PASS, f"SIP address: {settings.sip_uri}")

    if not settings.openai_webhook_secret:
        report(
            FAIL,
            "OPENAI_WEBHOOK_SECRET missing",
            "Create a realtime.call.incoming webhook and copy its signing secret",
        )
    else:
        report(PASS, "webhook signing secret present")


def check_business_template(settings) -> list[str]:
    print("\nBusiness profile template")
    path = settings.businesses_dir / "harborview-dental.yaml"
    try:
        profile = BusinessProfile.load(path)
        PublishedBusinessConfiguration.model_validate(profile.raw)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        report(FAIL, str(exc))
        return []

    numbers = profile.phone_numbers
    report(PASS, f"canonical template parses — example numbers: {numbers}")
    return numbers


def check_twilio(settings, configured_numbers: list[str]) -> None:
    print("\nTwilio")
    sid = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
    token = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
    if not sid or not token:
        report(
            FAIL,
            "Twilio credentials missing",
            "Needed to buy the number and route it to OpenAI. console.twilio.com > Account Info",
        )
        return

    try:
        from twilio.base.exceptions import TwilioRestException
        from twilio.rest import Client
    except ImportError:
        report(FAIL, "twilio package not installed")
        return

    client = Client(sid, token)
    try:
        account = client.api.accounts(sid).fetch()
    except TwilioRestException as exc:
        report(FAIL, f"Twilio rejected the credentials ({exc.msg})")
        return
    report(PASS, f"account '{account.friendly_name}' status={account.status}")
    if account.type and account.type.lower() == "trial":
        report(
            WARN,
            "this is a trial account",
            "Trial accounts restrict inbound calling. Upgrade before testing",
        )

    owned = {}
    for number in client.incoming_phone_numbers.list(limit=50):
        owned[digits(number.phone_number)] = number.phone_number
    if owned:
        report(PASS, f"numbers owned: {', '.join(sorted(owned.values()))}")
    else:
        report(
            FAIL,
            "no phone numbers on this account",
            "Run: python scripts/twilio_setup.py provision --country US",
        )

    expected = settings.sip_uri
    routed: set[str] = set()
    trunk_found = False
    for trunk in client.trunking.v1.trunks.list(limit=50):
        ctx = client.trunking.v1.trunks(trunk.sid)
        urls = [u.sip_url for u in ctx.origination_urls.list(limit=20) if u.enabled]
        if not any(u.strip() == expected for u in urls):
            continue
        trunk_found = True
        for phone in ctx.phone_numbers.list(limit=50):
            routed.add(digits(phone.phone_number))
        report(PASS, f"trunk '{trunk.friendly_name}' points at your OpenAI project")

    if not trunk_found:
        report(
            FAIL,
            "no enabled SIP trunk routes to your OpenAI project",
            "Run: python scripts/twilio_setup.py provision --country US",
        )
    elif not routed:
        report(FAIL, "trunk exists but has no phone number attached")
    else:
        report(PASS, f"numbers routed to OpenAI: {len(routed)}")

    # The number a caller dials must match what the business config claims.
    for wanted in configured_numbers:
        if digits(wanted) not in owned:
            report(
                WARN,
                f"{wanted} is in a business config but not owned on Twilio",
                "Update business.phone_numbers to the number you actually bought",
            )
        elif digits(wanted) not in routed:
            report(WARN, f"{wanted} is owned but not attached to the OpenAI trunk")


def main() -> None:
    settings = load_settings()
    print("Call Agent preflight")
    check_openai(settings)
    numbers = check_business_template(settings)
    check_twilio(settings, numbers)

    fails = sum(1 for s, _ in results if s == FAIL)
    warns = sum(1 for s, _ in results if s == WARN)
    print(f"\n{len(results)} checks: {fails} failed, {warns} warnings")
    if fails:
        print("Fix the failures above before calling.")
        sys.exit(1)
    print("Ready. Start the server, start ngrok, then dial your number.")


if __name__ == "__main__":
    main()
