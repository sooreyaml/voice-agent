from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.concurrency import run_in_threadpool

from .business import BusinessProfile
from .domains.integrations.base import Booking
from .domains.integrations.constants import (
    MAX_SLOTS_RETURNED,
    TOOL_TIMEOUT_SECONDS,
)
from .domains.integrations.registry import (
    DEFAULT_DURATION_MINUTES,
    calendar_tool_schemas,
    crm_tool_schemas,
)

if TYPE_CHECKING:  # avoids a circular import at runtime
    from .session import CallSession

logger = logging.getLogger(__name__)

INTENTS = [
    "book_appointment",
    "reschedule_or_cancel",
    "pricing_question",
    "opening_hours_or_location",
    "existing_customer_query",
    "complaint",
    "sales_or_supplier",
    "other",
]


def tool_schemas(
    profile: BusinessProfile, *, calendar: bool = False, crm: bool = False
) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = [
        {
            "type": "function",
            "name": "capture_caller_need",
            "description": (
                "Record what the caller wants so the team can act on it. Call this "
                "as soon as you understand their reason for calling, and call it "
                "again with fuller information if you learn more."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "enum": INTENTS,
                        "description": "The closest match to why they called.",
                    },
                    "details": {
                        "type": "string",
                        "description": (
                            "One or two sentences in your own words describing what "
                            "the caller asked for."
                        ),
                    },
                    "caller_name": {
                        "type": "string",
                        "description": "If they gave it.",
                    },
                    "callback_number": {
                        "type": "string",
                        "description": "Best number to reach them, as they said it.",
                    },
                    "preferred_time": {
                        "type": "string",
                        "description": "Any date or time preference they mentioned.",
                    },
                    "urgency": {
                        "type": "string",
                        "enum": ["low", "normal", "high", "emergency"],
                    },
                },
                "required": ["intent", "details"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "end_call",
            "description": (
                "Hang up. Only call this after you have said goodbye and the caller "
                "has nothing further."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Short note on how the call concluded.",
                    }
                },
                "required": ["reason"],
                "additionalProperties": False,
            },
        },
    ]

    if profile.transfer_number:
        tools.insert(
            1,
            {
                "type": "function",
                "name": "transfer_to_human",
                "description": (
                    "Put the caller through to a member of staff. Tell the caller you "
                    "are transferring them before calling this."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": "Why the call needs a human.",
                        }
                    },
                    "required": ["reason"],
                    "additionalProperties": False,
                },
            },
        )

    if calendar:
        # A connected calendar adds check_availability + book_appointment.
        tools[1:1] = calendar_tool_schemas()

    if crm:
        # A connected CRM adds find_customer + create_follow_up.
        tools[1:1] = crm_tool_schemas()

    return tools


_LEAD_FIELDS = (
    "intent",
    "caller_name",
    "callback_number",
    "urgency",
    "preferred_time",
    "details",
)


async def _emit_lead_created(
    session: CallSession, lead_id: int, args: dict[str, Any]
) -> None:
    """Queue a lead.created webhook for the org's registered endpoints. Best
    effort: a webhook problem must not affect the live call.
    """
    from .domains.webhooks import service as webhook_service
    from .domains.webhooks.constants import EVENT_LEAD_CREATED

    data = {"lead_id": lead_id, "call_id": session.call_id}
    data.update((field, args.get(field)) for field in _LEAD_FIELDS)
    try:
        await run_in_threadpool(
            webhook_service.emit_event,
            session.store,
            organization_id=session.organization_id,
            event_type=EVENT_LEAD_CREATED,
            dedupe_key=f"lead:{lead_id}",
            data=data,
            max_attempts=session.settings.webhook_max_attempts,
        )
    except Exception as exc:  # noqa: BLE001 - webhook delivery is best effort
        logger.warning(
            "lead.created webhook emit failed for %s: %s", session.call_id, exc
        )


async def _emit_appointment_booked(
    session: CallSession, booking: Booking, args: dict[str, Any]
) -> None:
    """Queue an appointment.booked webhook. Best effort, like lead.created."""
    from .domains.webhooks import service as webhook_service
    from .domains.webhooks.constants import EVENT_APPOINTMENT_BOOKED

    data = {
        "call_id": session.call_id,
        "booking_id": booking.id,
        "start": booking.start.isoformat(),
        "end": booking.end.isoformat(),
        "location": booking.location,
        "caller_name": args.get("caller_name"),
        "callback_number": args.get("callback_number"),
        "notes": args.get("notes"),
    }
    try:
        await run_in_threadpool(
            webhook_service.emit_event,
            session.store,
            organization_id=session.organization_id,
            event_type=EVENT_APPOINTMENT_BOOKED,
            dedupe_key=f"appointment:{booking.id}",
            data=data,
            max_attempts=session.settings.webhook_max_attempts,
        )
    except Exception as exc:  # noqa: BLE001 - webhook delivery is best effort
        logger.warning(
            "appointment.booked webhook emit failed for %s: %s",
            session.call_id,
            exc,
        )


def _business_timezone(profile: BusinessProfile) -> tuple[Any, str]:
    """(tzinfo, IANA name) for the business, falling back to UTC."""
    name = (profile.timezone or "").strip()
    if not name:
        return UTC, "UTC"
    try:
        return ZoneInfo(name), name
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("unknown business timezone %r; using UTC", name)
        return UTC, "UTC"


def _spoken_when(local: datetime) -> str:
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    day = local.strftime("%A %d %B").replace(" 0", " ")
    if local.minute:
        return f"{day} at {hour}:{local.minute:02d} {suffix}"
    return f"{day} at {hour} {suffix}"


_PART_OF_DAY = {
    "morning": (0, 12),
    "afternoon": (12, 17),
    "evening": (17, 24),
}


async def _call_provider(fn: Any, /, **kwargs: Any) -> Any:
    """Run a synchronous provider call off the loop under a hard timeout."""
    return await asyncio.wait_for(run_in_threadpool(fn, **kwargs), TOOL_TIMEOUT_SECONDS)


_CALENDAR_FALLBACK = {
    "status": "unavailable",
    "instruction": (
        "The calendar could not be reached just now. Apologise briefly, then use "
        "capture_caller_need to take the caller's name, number and the day and "
        "time they want so the team can call back to confirm."
    ),
}


async def _check_availability(
    session: CallSession, args: dict[str, Any]
) -> dict[str, Any]:
    if session.calendar is None:  # tool should not be offered, but be safe
        return _CALENDAR_FALLBACK
    try:
        day = date.fromisoformat(str(args.get("date", "")).strip())
    except ValueError:
        return {
            "status": "error",
            "instruction": (
                "Ask the caller which day they would like, then call "
                "check_availability again with that date."
            ),
        }

    tzinfo, _tz_name = _business_timezone(session.profile)
    midnight = datetime.combine(day, time.min, tzinfo=tzinfo)
    start_local = max(midnight, datetime.now(tzinfo))
    end_local = midnight + timedelta(days=1)
    if end_local <= start_local:
        return {
            "status": "no_slots",
            "instruction": "That day is already past. Ask the caller for another day.",
        }

    window = _PART_OF_DAY.get(str(args.get("part_of_day") or "any"))
    try:
        slots = await _call_provider(
            session.calendar.available_slots,
            start=start_local.astimezone(UTC),
            end=end_local.astimezone(UTC),
            duration_minutes=DEFAULT_DURATION_MINUTES,
        )
    except Exception as exc:  # noqa: BLE001 - provider errors must not stall the caller
        logger.warning("check_availability failed for %s: %s", session.call_id, exc)
        await session.note(f"calendar availability lookup failed: {exc}")
        return _CALENDAR_FALLBACK

    offered: list[dict[str, str]] = []
    for slot in slots:
        local = slot.start.astimezone(tzinfo)
        if window and not (window[0] <= local.hour < window[1]):
            continue
        offered.append(
            {"start": local.isoformat(timespec="minutes"), "when": _spoken_when(local)}
        )
        if len(offered) >= MAX_SLOTS_RETURNED:
            break

    await session.note(
        f"availability {args.get('date')} ({args.get('part_of_day') or 'any'}): "
        f"{len(offered)} slot(s)"
    )
    if not offered:
        return {
            "status": "no_slots",
            "instruction": (
                "There is nothing free then. Offer the caller another day, or use "
                "capture_caller_need to take a callback."
            ),
        }
    return {
        "status": "ok",
        "slots": offered,
        "instruction": (
            "Offer the caller two or three of these times in a natural way. When "
            "they pick one, confirm it and call book_appointment with that exact "
            "start value."
        ),
    }


async def _book_appointment(
    session: CallSession, args: dict[str, Any]
) -> dict[str, Any]:
    caller_name = str(args.get("caller_name") or "").strip()
    callback = str(args.get("callback_number") or "").strip()
    notes = str(args.get("notes") or "").strip() or None
    start_raw = str(args.get("start") or "").strip()

    tzinfo, tz_name = _business_timezone(session.profile)
    start_utc: datetime | None = None
    try:
        parsed = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tzinfo)
        start_utc = parsed.astimezone(UTC)
    except ValueError:
        start_utc = None

    if session.calendar is not None and start_utc is not None:
        try:
            booking = await _call_provider(
                session.calendar.create_booking,
                start=start_utc,
                duration_minutes=DEFAULT_DURATION_MINUTES,
                name=caller_name or "Phone caller",
                phone=callback or None,
                notes=notes,
                timezone=tz_name,
            )
        except Exception as exc:  # noqa: BLE001 - provider errors fall back to a lead
            logger.warning("book_appointment failed for %s: %s", session.call_id, exc)
            return await _capture_booking_fallback(
                session,
                caller_name,
                callback,
                notes,
                start_raw,
                reason=f"calendar booking failed: {exc}",
            )
        spoken = _spoken_when(booking.start.astimezone(tzinfo))
        lead_args = {
            "intent": "book_appointment",
            "details": f"Booked {spoken}." + (f" {notes}" if notes else ""),
            "caller_name": caller_name or None,
            "callback_number": callback or None,
            "preferred_time": spoken,
        }
        lead_id = await run_in_threadpool(
            session.store.add_lead,
            session.organization_id,
            session.call_id,
            lead_args,
        )
        session.intent = "book_appointment"
        await session.note(f"booked appointment {booking.id} for {spoken}")
        await _emit_lead_created(session, lead_id, lead_args)
        await _emit_appointment_booked(session, booking, args)
        return {
            "status": "booked",
            "when": spoken,
            "booking_id": booking.id,
            "instruction": (
                "Confirm the day and time back to the caller and tell them they "
                "will get a confirmation, then ask if there is anything else."
            ),
        }

    return await _capture_booking_fallback(
        session,
        caller_name,
        callback,
        notes,
        start_raw,
        reason="calendar unavailable or start time unclear",
    )


async def _capture_booking_fallback(
    session: CallSession,
    caller_name: str,
    callback: str,
    notes: str | None,
    start_raw: str,
    *,
    reason: str,
) -> dict[str, Any]:
    detail = "Wants to book an appointment"
    if start_raw:
        detail += f" around {start_raw}"
    detail += ". Booking could not be completed on the call; needs a manual callback."
    if notes:
        detail += f" Notes: {notes}"
    lead_args = {
        "intent": "book_appointment",
        "details": detail,
        "caller_name": caller_name or None,
        "callback_number": callback or None,
        "preferred_time": start_raw or None,
    }
    lead_id = await run_in_threadpool(
        session.store.add_lead,
        session.organization_id,
        session.call_id,
        lead_args,
    )
    session.intent = "book_appointment"
    await session.note(f"booking not completed ({reason}); captured for follow-up")
    await _emit_lead_created(session, lead_id, lead_args)
    return {
        "status": "failed",
        "instruction": (
            "Tell the caller you have noted the request and someone will call back "
            "shortly to confirm the exact time. Then ask if there is anything else."
        ),
    }


_CRM_UNAVAILABLE = {
    "status": "unavailable",
    "instruction": (
        "Could not reach the customer system just now. Continue the call "
        "normally and take the caller's details as usual."
    ),
}


async def _find_customer(session: CallSession, args: dict[str, Any]) -> dict[str, Any]:
    if session.crm is None:  # tool should not be offered, but be safe
        return _CRM_UNAVAILABLE
    phone = str(args.get("phone") or session.from_number or "").strip() or None
    email = str(args.get("email") or "").strip() or None
    if not phone and not email:
        return {
            "status": "not_found",
            "instruction": "No detail to search on. Continue normally.",
        }
    try:
        contact = await _call_provider(
            session.crm.find_contact, phone=phone, email=email
        )
    except Exception as exc:  # noqa: BLE001 - provider errors must not stall the caller
        logger.warning("find_customer failed for %s: %s", session.call_id, exc)
        await session.note(f"crm lookup failed: {exc}")
        return _CRM_UNAVAILABLE

    if contact is None:
        await session.note("crm lookup: no match")
        return {
            "status": "not_found",
            "instruction": (
                "No existing record. Continue normally and capture their details "
                "with capture_caller_need or create_follow_up."
            ),
        }
    await session.note(f"crm lookup: matched {contact.name or contact.id}")
    if contact.name:
        return {
            "status": "found",
            "name": contact.name,
            "instruction": (
                "There is an existing record. Greet them by name if it feels "
                "natural; do not read their stored details back to them."
            ),
        }
    return {
        "status": "found",
        "instruction": "There is an existing record. Continue normally.",
    }


async def _create_follow_up(
    session: CallSession, args: dict[str, Any]
) -> dict[str, Any]:
    summary = str(args.get("summary") or "").strip()
    if not summary:
        return {
            "status": "error",
            "instruction": "Ask what the follow-up is for, then call this again.",
        }
    caller_name = str(args.get("caller_name") or "").strip() or None
    callback = str(args.get("callback_number") or "").strip() or None
    lead_args = {
        "intent": session.intent or "other",
        "details": summary,
        "caller_name": caller_name,
        "callback_number": callback,
    }
    lead_id = await run_in_threadpool(
        session.store.add_lead,
        session.organization_id,
        session.call_id,
        lead_args,
    )
    await session.note(f"follow-up logged: {summary}")
    # The CRM contact/note/task are created after the call by the worker sync,
    # which picks this lead up. Doing it here would risk stalling the caller.
    await _emit_lead_created(session, lead_id, lead_args)
    return {
        "status": "noted",
        "instruction": (
            "Confirm to the caller that it is logged and someone will follow up, "
            "then ask if there is anything else."
        ),
    }


async def dispatch(
    session: CallSession, name: str, args: dict[str, Any]
) -> dict[str, Any]:
    """Run a tool call and return the payload handed back to the model."""
    if name == "capture_caller_need":
        lead_id = await run_in_threadpool(
            session.store.add_lead,
            session.organization_id,
            session.call_id,
            args,
        )
        await session.note(
            f"captured need: {args.get('intent')} — {args.get('details', '')}"
        )
        session.intent = args.get("intent") or session.intent
        await _emit_lead_created(session, lead_id, args)
        return {
            "status": "recorded",
            "instruction": (
                "The team has this. Do not read it back in full. Just confirm "
                "briefly that someone will follow up, then ask if there is "
                "anything else."
            ),
        }

    if name == "transfer_to_human":
        target = session.profile.transfer_number
        if not target:
            return {
                "status": "unavailable",
                "instruction": (
                    "No one is available to transfer to. Apologise, offer to take a "
                    "message instead."
                ),
            }
        session.outcome = "transferred"
        await session.note(f"transfer requested: {args.get('reason', '')}")
        try:
            await session.calls.refer(session.call_id, f"tel:{target}")
        except Exception as exc:  # noqa: BLE001 - a failed transfer keeps call alive
            logger.warning("transfer failed for %s: %s", session.call_id, exc)
            session.outcome = "transfer_failed"
            return {
                "status": "failed",
                "instruction": (
                    "The transfer did not go through. Apologise and offer to take a "
                    "message so someone can call them back."
                ),
            }
        return {"status": "transferring"}

    if name == "check_availability":
        return await _check_availability(session, args)

    if name == "book_appointment":
        return await _book_appointment(session, args)

    if name == "find_customer":
        return await _find_customer(session, args)

    if name == "create_follow_up":
        return await _create_follow_up(session, args)

    if name == "end_call":
        session.outcome = session.outcome or "completed"
        await session.note(f"agent ended call: {args.get('reason', '')}")
        session.request_hangup()
        return {"status": "ending"}

    logger.warning("unknown tool %s", name)
    return {
        "status": "error",
        "instruction": "That tool does not exist. Continue the call.",
    }
