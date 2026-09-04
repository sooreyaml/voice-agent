"""Build the first agent profile a new organization gets.

Clones ``businesses/_default.yaml`` and stamps the tenant's identity onto it, the
same way ``app.domains.organizations.seeding.build_profile`` does for the CLI
seed path. The result is published when the number is provisioned.

``build_default_profile`` is the bare version (name + timezone only), still used
by the legacy backfill path. ``build_onboarding_profile`` is what the gated
signup flow uses: it folds in the owner's completed business profile so the very
first published agent is not a placeholder.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from app.business import BusinessProfile

DEFAULT_TIMEZONE = "Europe/London"
DEFAULT_AGENT_NAME = "Alex"


def build_default_profile(
    *,
    template_path: Path,
    business_name: str,
    slug: str,
    phone_number: str,
    timezone: str = DEFAULT_TIMEZONE,
    agent_name: str = DEFAULT_AGENT_NAME,
    greeting: str | None = None,
) -> BusinessProfile:
    source = BusinessProfile.load(template_path)
    raw = copy.deepcopy(source.raw)
    business = raw.setdefault("business", {})
    agent = raw.setdefault("agent", {})

    business["name"] = business_name.strip()
    business["timezone"] = timezone
    business["phone_numbers"] = [phone_number]

    agent.setdefault("name", agent_name)
    agent["greeting"] = greeting or (
        f"Thanks for calling {business_name.strip()}. This is "
        f"{agent.get('name', agent_name)}, how can I help you today?"
    )

    return BusinessProfile(raw=raw, slug=slug)


def build_onboarding_profile(
    *,
    template_path: Path,
    intake: dict[str, Any],
    slug: str,
    phone_number: str,
) -> BusinessProfile:
    """Build the first agent from the owner's completed business-profile intake.

    Unlike :func:`build_default_profile` the result reflects the real business —
    its name, what it does, its timezone and contact details — so an un-edited
    agent already answers sensibly.
    """
    source = BusinessProfile.load(template_path)
    raw = copy.deepcopy(source.raw)
    business = raw.setdefault("business", {})
    agent = raw.setdefault("agent", {})
    contact = raw.setdefault("contact", {})

    name = str(intake.get("business_name") or intake.get("legal_name") or "").strip()
    business["name"] = name
    business["timezone"] = intake["timezone"]
    business["phone_numbers"] = [phone_number]
    what_you_do = str(intake.get("what_you_do") or "").strip()
    if what_you_do:
        business["what_we_do"] = what_you_do
    industry = str(intake.get("industry") or "").strip()
    if industry:
        business["industry"] = industry

    if intake.get("contact_email"):
        contact.setdefault("email", str(intake["contact_email"]))
    if intake.get("contact_phone"):
        contact.setdefault("phone", str(intake["contact_phone"]))

    agent_name = agent.setdefault("name", DEFAULT_AGENT_NAME)
    agent["greeting"] = (
        f"Thanks for calling {name}. This is {agent_name}, "
        "how can I help you today?"
    )

    return BusinessProfile(raw=raw, slug=slug)
