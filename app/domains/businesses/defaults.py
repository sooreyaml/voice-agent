"""Build the first agent profile a new organization gets at signup.

Clones ``businesses/_default.yaml`` and stamps the tenant's identity onto it, the
same way ``app.domains.organizations.seeding.build_profile`` does for the CLI
seed path. The result is published immediately so the tenant's number is live
before they have touched any settings.
"""

from __future__ import annotations

import copy
from pathlib import Path

from app.business import BusinessProfile

DEFAULT_TIMEZONE = "America/New_York"
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
