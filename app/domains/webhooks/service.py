"""Outbound webhook logic: endpoint config, event fan-out, signed delivery,
and retry scheduling. FastAPI-free so the worker can reuse it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.store import Store

from . import signing
from .constants import (
    ATTEMPT_HEADER,
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DELIVERY_ID_HEADER,
    DELIVERY_TIMEOUT_SECONDS,
    EVENT_ID_HEADER,
    EVENT_TYPE_HEADER,
    RESPONSE_SNIPPET_CHARS,
    SECRET_PREFIX,
    SIGNATURE_HEADER,
    STATUS_DEAD,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    USER_AGENT,
)

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def new_secret() -> str:
    return SECRET_PREFIX + secrets.token_urlsafe(32)


def backoff_delay(attempt: int) -> timedelta:
    """Seconds to wait before ``attempt`` (1-indexed). Exponential, capped,
    with a little deterministic-free jitter so a fleet does not sync up.
    """
    raw = BACKOFF_BASE_SECONDS * (2 ** max(attempt - 1, 0))
    capped = min(raw, BACKOFF_CAP_SECONDS)
    jitter = secrets.randbelow(max(int(capped * 0.2), 1))
    return timedelta(seconds=capped + jitter)


# -- event emission --------------------------------------------------------


def emit_event(
    store: Store,
    *,
    organization_id: str,
    event_type: str,
    dedupe_key: str,
    data: dict[str, Any],
    occurred_at: datetime | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> str | None:
    """Record an event and queue a delivery per subscribed endpoint. Safe to
    call when the org has no endpoints (returns None, writes nothing).
    """
    envelope = {
        "id": None,  # filled below once the event id is known
        "type": event_type,
        "occurred_at": (occurred_at or _utcnow()).isoformat(),
        "organization_id": organization_id,
        "data": data,
    }
    payload = json.dumps(envelope, default=str)
    event_id = store.enqueue_webhook_event(
        organization_id,
        event_type,
        dedupe_key,
        payload,
        occurred_at or _utcnow(),
        max_attempts,
    )
    if event_id is None:
        return None
    # Re-serialise with the real id now that a row exists.
    envelope["id"] = event_id
    store.execute(
        "UPDATE webhook_events SET payload = ? WHERE id = ?",
        (json.dumps(envelope, default=str), event_id),
    )
    return event_id


# -- delivery ------------------------------------------------------------


async def _post(
    client: httpx.AsyncClient, row: dict[str, Any], timeout: float
) -> tuple[int | None, str | None, str | None, int]:
    """Returns (status_code, error, response_snippet, duration_ms)."""
    body = str(row["event_payload"]).encode("utf-8")
    timestamp = int(time.time())
    headers = {
        "content-type": "application/json",
        "user-agent": USER_AGENT,
        SIGNATURE_HEADER: signing.compute(row["endpoint_secret"], timestamp, body),
        EVENT_TYPE_HEADER: str(row["event_type"]),
        EVENT_ID_HEADER: str(row["webhook_event_id"]),
        DELIVERY_ID_HEADER: str(row["id"]),
        ATTEMPT_HEADER: str(int(row["attempts"]) + 1),
    }
    started = time.monotonic()
    try:
        response = await client.post(
            str(row["endpoint_url"]), content=body, headers=headers, timeout=timeout
        )
    except httpx.HTTPError as exc:
        return None, f"{type(exc).__name__}: {exc}"[:500], None, _ms(started)
    snippet = response.text[:RESPONSE_SNIPPET_CHARS] if response.text else None
    error = None if response.is_success else f"HTTP {response.status_code}"
    return response.status_code, error, snippet, _ms(started)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


async def deliver_one(
    store: Store,
    row: dict[str, Any],
    client: httpx.AsyncClient,
    *,
    timeout: float = DELIVERY_TIMEOUT_SECONDS,
) -> str:
    """Attempt one claimed delivery and persist the outcome. Returns the new
    status.
    """
    attempt = int(row["attempts"]) + 1
    max_attempts = int(row["max_attempts"])
    status_code, error, snippet, duration_ms = await _post(client, row, timeout)

    if status_code is not None and 200 <= status_code < 300:
        status, next_at = STATUS_SUCCEEDED, None
    elif attempt >= max_attempts:
        status, next_at = STATUS_DEAD, None
    else:
        status, next_at = STATUS_FAILED, _utcnow() + backoff_delay(attempt)

    await _to_thread(
        store.record_webhook_attempt,
        row["id"],
        attempt=attempt,
        status=status,
        status_code=status_code,
        error=error,
        duration_ms=duration_ms,
        response_snippet=snippet,
        next_attempt_at=next_at,
    )
    if status == STATUS_DEAD:
        logger.warning(
            "webhook delivery %s dead after %d attempts (last: %s)",
            row["id"],
            attempt,
            error,
        )
    return status


async def process_due_deliveries(
    store: Store,
    client: httpx.AsyncClient,
    *,
    batch_size: int = 20,
    stale_lock: timedelta = timedelta(seconds=300),
    timeout: float = DELIVERY_TIMEOUT_SECONDS,
) -> int:
    """Claim and attempt one batch of due deliveries. Returns how many were
    processed (0 means the queue is idle).
    """
    claimed = await _to_thread(
        store.claim_webhook_deliveries, batch_size, _utcnow() - stale_lock
    )
    for row in claimed:
        try:
            await deliver_one(store, row, client, timeout=timeout)
        except Exception:
            # Never let one bad row stall the whole worker pass.
            logger.exception("webhook delivery %s crashed", row.get("id"))
            await _to_thread(
                store.record_webhook_attempt,
                row["id"],
                attempt=int(row["attempts"]) + 1,
                status=STATUS_FAILED,
                status_code=None,
                error="worker exception",
                duration_ms=None,
                response_snippet=None,
                next_attempt_at=_utcnow() + backoff_delay(int(row["attempts"]) + 1),
            )
    return len(claimed)


async def _to_thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)
