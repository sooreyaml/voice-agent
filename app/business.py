from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

logger = logging.getLogger(__name__)


@dataclass
class BusinessProfile:
    """One immutable configuration snapshot used by a call."""

    raw: dict[str, Any] = field(default_factory=dict)
    slug: str = ""
    organization_id: str = ""
    profile_id: str = ""
    version_id: str = ""
    version_number: int = 0

    @classmethod
    def load(cls, path: Path) -> BusinessProfile:
        if not path.exists():
            raise FileNotFoundError(f"Business config not found at {path}.")
        with path.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return cls(raw=data, slug=path.stem)

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name) or {}
        return value if isinstance(value, dict) else {}

    @property
    def name(self) -> str:
        return self.section("business").get("name", "the business")

    @property
    def agent_name(self) -> str:
        return self.section("agent").get("name", "the assistant")

    @property
    def voice(self) -> str | None:
        return self.section("agent").get("voice")

    @property
    def greeting(self) -> str:
        return self.section("agent").get(
            "greeting", f"Thanks for calling {self.name}. How can I help you today?"
        )

    @property
    def transfer_number(self) -> str:
        return str(self.section("contact").get("transfer_to") or "").strip()

    @property
    def phone_numbers(self) -> list[str]:
        """The inbound numbers that should ring this business."""
        values = self.section("business").get("phone_numbers") or []
        return [str(v) for v in values]

    @property
    def notify_webhook(self) -> str:
        return str(self.section("business").get("notify_webhook") or "").strip()

    @property
    def notify_email(self) -> str:
        return str(self.section("business").get("notify_email") or "").strip()

    @property
    def timezone(self) -> str:
        return str(self.section("business").get("timezone") or "").strip()

    @property
    def knowledge(self) -> str:
        """Optional free-prose context for anything the schema does not cover."""
        return str(self.raw.get("knowledge") or "").strip()

    def local_time(self) -> str:
        """Human-readable current time where the business is, for the prompt."""
        try:
            now = (
                datetime.now(ZoneInfo(self.timezone))
                if self.timezone
                else datetime.now(UTC)
            )
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("unknown timezone %r for %s", self.timezone, self.name)
            now = datetime.now(UTC)
        return now.strftime("%A %d %B %Y, %I:%M %p").replace(" 0", " ")

    @property
    def services(self) -> list[dict[str, Any]]:
        items = self.raw.get("services") or []
        return [i for i in items if isinstance(i, dict)]

    @property
    def faqs(self) -> list[dict[str, Any]]:
        items = self.raw.get("faqs") or []
        return [i for i in items if isinstance(i, dict)]


def _clean(value: Any) -> str:
    return " ".join(str(value).split())


def _render_knowledge(profile: BusinessProfile) -> str:
    """Flatten the YAML into prose the model reads well when spoken aloud."""
    lines: list[str] = []
    business = profile.section("business")

    lines.append(f"Business name: {profile.name}")
    if business.get("what_we_do"):
        lines.append(f"What the business does: {_clean(business['what_we_do'])}")
    if business.get("timezone"):
        lines.append(f"Local timezone: {business['timezone']}")

    hours = profile.section("hours")
    if hours:
        lines.append("\nOpening hours:")
        for day, value in hours.items():
            if day == "notes":
                continue
            lines.append(f"- {day.capitalize()}: {_clean(value)}")
        if hours.get("notes"):
            lines.append(f"- Note: {_clean(hours['notes'])}")

    location = profile.section("location")
    if location:
        lines.append("\nLocation:")
        if location.get("address"):
            lines.append(f"- Address: {_clean(location['address'])}")
        if location.get("directions"):
            lines.append(f"- Getting there: {_clean(location['directions'])}")

    contact = profile.section("contact")
    if contact:
        lines.append("\nContact details you may share with the caller:")
        for key in ("phone", "email", "website"):
            if contact.get(key):
                lines.append(f"- {key.capitalize()}: {contact[key]}")

    if profile.services:
        lines.append("\nServices and prices. These are the only prices you may quote:")
        for item in profile.services:
            parts = [f"- {_clean(item.get('name', 'Service'))}"]
            if item.get("price"):
                parts.append(f"price {_clean(item['price'])}")
            if item.get("duration"):
                parts.append(f"takes {_clean(item['duration'])}")
            line = ", ".join(parts)
            if item.get("notes"):
                line += f". {_clean(item['notes'])}"
            lines.append(line)

    if profile.faqs:
        lines.append("\nCommon questions and the approved answers:")
        for item in profile.faqs:
            lines.append(f"- Q: {_clean(item.get('question', ''))}")
            lines.append(f"  A: {_clean(item.get('answer', ''))}")

    if profile.knowledge:
        lines.append("\nAdditional background:\n" + profile.knowledge)

    return "\n".join(lines)


def render_instructions(profile: BusinessProfile) -> str:
    agent = profile.section("agent")
    role = agent.get("role", "receptionist")
    style = _clean(agent.get("style", "Warm, efficient and concise."))
    guardrails = profile.section("guardrails")
    never = [_clean(x) for x in (guardrails.get("never") or [])]
    transfer_when = [_clean(x) for x in (guardrails.get("transfer_when") or [])]

    sections: list[str] = [
        (
            f"You are {profile.agent_name}, the {role} for {profile.name}. "
            f"You are speaking with a caller on the telephone. {style}"
        ),
        (
            "HOW TO SPEAK\n"
            "You are on a voice call, so everything you say is heard, not read. "
            "Never use markdown, bullet points, lists, emoji or special characters. "
            "Keep each turn to one or two sentences. Ask only one question at a time, "
            "then stop and let the caller answer. Say numbers, prices and times the way "
            "a person would say them out loud. If the caller interrupts you, stop "
            "immediately and listen. If the line is silent for a while, ask once "
            "whether they are still there."
        ),
        (
            "WHAT YOU ARE HERE TO DO\n"
            "First, find out what the caller needs. Answer their questions about the "
            "business using only the information below. Once you understand what they "
            "want, call the capture_caller_need tool so the team has a record of it, "
            "including their name and the best number to reach them on. Read any phone "
            "number or email address back to the caller to confirm you heard it right."
        ),
        (
            "STAYING HONEST\n"
            "The information below is the only thing you know about this business. If "
            "the caller asks something it does not cover, say plainly that you do not "
            "have that detail to hand, offer to take a message so someone can follow "
            "up, or offer to put them through. Never guess, never improvise a fact, and "
            "never invent a price, a policy or an availability."
        ),
    ]

    if never:
        sections.append(
            "YOU MUST NEVER DO ANY OF THE FOLLOWING\n"
            + "\n".join(f"- {x}" for x in never)
        )

    if transfer_when:
        sections.append(
            "TRANSFER THE CALL by calling transfer_to_human when any of these is true. "
            "Tell the caller you are putting them through before you call the tool.\n"
            + "\n".join(f"- {x}" for x in transfer_when)
        )

    sections.append(
        "ENDING THE CALL\n"
        "When the caller's business is done and they have nothing else, thank them "
        "briefly, say goodbye, and call the end_call tool. Do not call it while the "
        "caller may still be talking."
    )

    sections.append(
        "RIGHT NOW\n"
        f"It is currently {profile.local_time()} where the business is. Use this to "
        "work out whether you are open, and to understand what the caller means by "
        "today, tomorrow or this week. Never state a time or date you have not "
        "worked out from this."
    )

    sections.append("WHAT YOU KNOW ABOUT THE BUSINESS\n" + _render_knowledge(profile))

    return "\n\n".join(sections)


# --- Loading business templates from YAML -----------------------------------
#
# Inbound calls are routed against published database versions, not files (see
# app.domains.businesses.repository). YAML is only a template format used by
# explicit provisioning tools.


def load_profiles(directory: Path) -> list[BusinessProfile]:
    if not directory.exists():
        raise FileNotFoundError(
            f"No businesses directory at {directory}. Create it and add one YAML "
            "file per business."
        )
    profiles: list[BusinessProfile] = []
    for path in sorted(directory.glob("*.y*ml")):
        try:
            profiles.append(BusinessProfile.load(path))
        except (yaml.YAMLError, OSError):
            logger.exception("skipping unreadable business config %s", path)
    return profiles
