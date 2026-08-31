"""Maps a provider name to a client, and owns the voice-tool schemas.

The runtime asks :func:`build_provider` for a ready client and merges
:func:`calendar_tool_schemas` / :func:`crm_tool_schemas` into the tool list only
when the matching integration is connected (roadmap section 8).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .base import CalendarProvider, CrmProvider
from .constants import (
    DEFAULT_APPOINTMENT_MINUTES,
    PROVIDER_CAL_COM,
    PROVIDER_HUBSPOT,
    TOOL_TIMEOUT_SECONDS,
)
from .exceptions import UnknownProvider
from .providers.cal_com import CalComCalendar
from .providers.hubspot import HubSpotCrm

_Provider = CalendarProvider | CrmProvider
_Builder = Callable[[dict[str, Any], dict[str, Any], float], _Provider]


def _build_cal_com(
    credentials: dict[str, Any], settings: dict[str, Any], timeout: float
) -> CalComCalendar:
    return CalComCalendar(
        str(credentials.get("api_key", "")),
        event_type_id=int(settings["event_type_id"]),
        timezone=str(settings.get("timezone") or "UTC"),
        timeout=timeout,
    )


def _build_hubspot(
    credentials: dict[str, Any], settings: dict[str, Any], timeout: float
) -> HubSpotCrm:
    return HubSpotCrm(str(credentials.get("access_token", "")), timeout=timeout)


_BUILDERS: dict[str, _Builder] = {
    PROVIDER_CAL_COM: _build_cal_com,
    PROVIDER_HUBSPOT: _build_hubspot,
}


def build_provider(
    provider: str,
    credentials: dict[str, Any],
    settings: dict[str, Any],
    *,
    timeout: float = TOOL_TIMEOUT_SECONDS,
) -> _Provider:
    try:
        builder = _BUILDERS[provider]
    except KeyError as exc:
        raise UnknownProvider(provider) from exc
    return builder(credentials, settings, timeout)


CALENDAR_TOOL_NAMES = ("check_availability", "book_appointment")


def calendar_tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": "check_availability",
            "description": (
                "Look up open appointment times on the business calendar. Call "
                "this once the caller wants to book and has named a day. Offer "
                "the caller two or three of the times it returns."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {
                        "type": "string",
                        "description": (
                            "The date the caller wants, as YYYY-MM-DD. Work it "
                            "out from the current local time you were told."
                        ),
                    },
                    "part_of_day": {
                        "type": "string",
                        "enum": ["morning", "afternoon", "evening", "any"],
                        "description": "Narrow the search if the caller said so.",
                    },
                },
                "required": ["date"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "book_appointment",
            "description": (
                "Book a specific time on the business calendar. Only call this "
                "after check_availability and after the caller has agreed to a "
                "time. Tell the caller you are booking it before you call this."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {
                        "type": "string",
                        "description": (
                            "The exact start time to book, ISO 8601 "
                            "(YYYY-MM-DDTHH:MM). Use one of the times "
                            "check_availability returned."
                        ),
                    },
                    "caller_name": {
                        "type": "string",
                        "description": "The name to put on the booking.",
                    },
                    "callback_number": {
                        "type": "string",
                        "description": "Best number to reach the caller, as they said it.",
                    },
                    "notes": {
                        "type": "string",
                        "description": "What the appointment is for, in a few words.",
                    },
                },
                "required": ["start", "caller_name", "callback_number"],
                "additionalProperties": False,
            },
        },
    ]


CRM_TOOL_NAMES = ("find_customer", "create_follow_up")


def crm_tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": "find_customer",
            "description": (
                "Look the caller up in the CRM. Call this early if it would help "
                "to know whether they are an existing customer. Use the name it "
                "returns to greet them; do not read their details back."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {
                        "type": "string",
                        "description": (
                            "The number to search on. Defaults to the number "
                            "they are calling from if omitted."
                        ),
                    },
                    "email": {
                        "type": "string",
                        "description": "An email to search on, if the caller gives one.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "create_follow_up",
            "description": (
                "Log a follow-up for the team in the CRM. Call this once you "
                "understand what the caller needs and a person will have to act "
                "on it. Confirm to the caller that it is logged."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "One or two sentences on what needs doing.",
                    },
                    "caller_name": {
                        "type": "string",
                        "description": "The caller's name, if given.",
                    },
                    "callback_number": {
                        "type": "string",
                        "description": "Best number to reach them, as they said it.",
                    },
                },
                "required": ["summary"],
                "additionalProperties": False,
            },
        },
    ]


DEFAULT_DURATION_MINUTES = DEFAULT_APPOINTMENT_MINUTES
