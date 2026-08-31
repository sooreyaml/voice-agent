"""Deterministic builders for append-only usage ledger records."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any


def usage_event(
    *,
    organization_id: str,
    event_type: str,
    quantity: int,
    unit: str,
    source: str,
    idempotency_key: str,
    occurred_at: datetime,
    call_id: str | None = None,
    provider_cost_micros: int = 0,
    customer_charge_micros: int = 0,
    currency: str = "USD",
    provider_reference: str | None = None,
    reversal_of_event_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"call-agent:usage:{organization_id}:{source}:{idempotency_key}",
        )
    )
    return {
        "id": event_id,
        "organization_id": organization_id,
        "call_id": call_id,
        "event_type": event_type,
        "quantity": quantity,
        "unit": unit,
        "provider_cost_micros": provider_cost_micros,
        "customer_charge_micros": customer_charge_micros,
        "currency": currency.upper(),
        "source": source,
        "idempotency_key": idempotency_key,
        "provider_reference": provider_reference,
        "reversal_of_event_id": reversal_of_event_id,
        "metadata": json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True),
        "occurred_at": occurred_at,
    }


def insert_statement(event: dict[str, Any]) -> tuple[str, tuple[Any, ...]]:
    return (
        (
            "INSERT INTO usage_events"
            " (id, organization_id, call_id, event_type, quantity, unit,"
            " provider_cost_micros, customer_charge_micros, currency, source,"
            " idempotency_key, provider_reference, reversal_of_event_id, metadata,"
            " occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (organization_id, source, idempotency_key) DO NOTHING"
        ),
        (
            event["id"],
            event["organization_id"],
            event.get("call_id"),
            event["event_type"],
            event["quantity"],
            event["unit"],
            event.get("provider_cost_micros", 0),
            event.get("customer_charge_micros", 0),
            event.get("currency", "USD"),
            event["source"],
            event["idempotency_key"],
            event.get("provider_reference"),
            event.get("reversal_of_event_id"),
            event.get("metadata"),
            event["occurred_at"],
        ),
    )


def call_usage_events(
    *,
    organization_id: str,
    call_id: str,
    occurred_at: datetime,
    duration_seconds: int,
    realtime_model: str,
    realtime_usage: dict[str, int],
    realtime_cost_micros: int,
    transcription_enabled: bool,
    transcription_model: str,
    summary_model: str,
    summary_usage: dict[str, int],
    transferred: bool,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = [
        usage_event(
            organization_id=organization_id,
            call_id=call_id,
            event_type="twilio.call.duration",
            quantity=max(duration_seconds, 1),
            unit="second",
            source="twilio",
            idempotency_key=f"{call_id}:duration",
            provider_reference=call_id,
            occurred_at=occurred_at,
            metadata={"estimated": True},
        )
    ]
    for kind, quantity in sorted(realtime_usage.items()):
        if quantity <= 0:
            continue
        events.append(
            usage_event(
                organization_id=organization_id,
                call_id=call_id,
                event_type=f"openai.realtime.{kind}",
                quantity=quantity,
                unit="token",
                source="openai",
                idempotency_key=f"{call_id}:realtime:{kind}",
                provider_reference=call_id,
                occurred_at=occurred_at,
                metadata={"model": realtime_model},
            )
        )
    if realtime_cost_micros:
        events.append(
            usage_event(
                organization_id=organization_id,
                call_id=call_id,
                event_type="openai.realtime.estimated_cost",
                quantity=1,
                unit="call",
                source="openai",
                idempotency_key=f"{call_id}:realtime:estimated-cost",
                provider_cost_micros=realtime_cost_micros,
                provider_reference=call_id,
                occurred_at=occurred_at,
                metadata={"estimated": True, "model": realtime_model},
            )
        )
    if transcription_enabled:
        events.append(
            usage_event(
                organization_id=organization_id,
                call_id=call_id,
                event_type="openai.transcription.duration",
                quantity=max(duration_seconds, 1),
                unit="second",
                source="openai",
                idempotency_key=f"{call_id}:transcription:duration",
                provider_reference=call_id,
                occurred_at=occurred_at,
                metadata={"estimated": True, "model": transcription_model},
            )
        )
    for kind, quantity in sorted(summary_usage.items()):
        if quantity <= 0:
            continue
        events.append(
            usage_event(
                organization_id=organization_id,
                call_id=call_id,
                event_type=f"openai.summary.{kind}",
                quantity=quantity,
                unit="token",
                source="openai",
                idempotency_key=f"{call_id}:summary:{kind}",
                provider_reference=call_id,
                occurred_at=occurred_at,
                metadata={"model": summary_model},
            )
        )
    if transferred:
        events.append(
            usage_event(
                organization_id=organization_id,
                call_id=call_id,
                event_type="twilio.call.transfer",
                quantity=1,
                unit="event",
                source="twilio",
                idempotency_key=f"{call_id}:transfer",
                provider_reference=call_id,
                occurred_at=occurred_at,
            )
        )
    return events
