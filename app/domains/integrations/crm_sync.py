"""Post-call CRM synchronisation.

``enqueue_call_sync`` drops a job when a call finishes; ``process_due_crm_jobs``
(run from the background worker) drains the queue — upsert the caller as a
contact, attach a note with the call summary, and open a follow-up task per
captured lead. Each step is guarded by the persisted ``result`` so a retry after
a partial failure does not duplicate work.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from app.store import Store

from . import service
from .base import ProviderError
from .constants import (
    CRM_PROVIDERS,
    CRM_SYNC_BACKOFF_BASE_SECONDS,
    CRM_SYNC_BACKOFF_CAP_SECONDS,
    CRM_SYNC_MAX_ATTEMPTS,
    CRM_SYNC_STALE_LOCK_SECONDS,
)
from .crypto import CredentialCipher

logger = logging.getLogger(__name__)

KIND_CALL = "call"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_DEAD = "dead"


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _backoff(attempt: int) -> timedelta:
    raw = CRM_SYNC_BACKOFF_BASE_SECONDS * (2 ** max(attempt - 1, 0))
    capped = min(raw, CRM_SYNC_BACKOFF_CAP_SECONDS)
    return timedelta(seconds=capped + secrets.randbelow(max(int(capped * 0.2), 1)))


def enqueue_call_sync(
    store: Store,
    organization_id: str,
    call_id: str,
    *,
    max_attempts: int = CRM_SYNC_MAX_ATTEMPTS,
) -> str | None:
    """Queue a post-call CRM push if the org has a CRM connected. Best effort;
    returns the job id, or None when there is nothing to sync to.
    """
    crm = next(
        (
            row
            for row in store.active_integration_connections(organization_id)
            if str(row["provider"]) in CRM_PROVIDERS
        ),
        None,
    )
    if crm is None:
        return None
    return store.enqueue_crm_sync_job(
        organization_id, str(crm["provider"]), call_id, KIND_CALL, max_attempts
    )


# -- draining -------------------------------------------------------


def _load_result(job: dict[str, Any]) -> dict[str, Any]:
    try:
        decoded = json.loads(job.get("result") or "{}")
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _persist(store: Store, job: dict[str, Any], result: dict[str, Any]) -> None:
    store.update_crm_sync_result(str(job["id"]), json.dumps(result, default=str))


def _finish(
    store: Store, job: dict[str, Any], status: str, *, result: dict[str, Any] | None
) -> None:
    store.finish_crm_sync_job(
        str(job["id"]),
        status=status,
        attempts=int(job["attempts"]) + 1,
        next_attempt_at=None,
        last_error=None,
        result=json.dumps(result, default=str) if result is not None else None,
    )


def _retry_or_dead(
    store: Store, job: dict[str, Any], error: str, *, retryable: bool
) -> None:
    attempts = int(job["attempts"]) + 1
    if not retryable or attempts >= int(job["max_attempts"]):
        store.finish_crm_sync_job(
            str(job["id"]),
            status=STATUS_DEAD,
            attempts=attempts,
            next_attempt_at=None,
            last_error=error[:1000],
            result=None,
        )
        logger.warning(
            "crm sync job %s dead after %d attempts: %s",
            job["id"],
            attempts,
            error,
        )
        return
    store.finish_crm_sync_job(
        str(job["id"]),
        status=STATUS_FAILED,
        attempts=attempts,
        next_attempt_at=_utcnow() + _backoff(attempts),
        last_error=error[:1000],
        result=None,
    )


def _note_body(detail: dict[str, Any]) -> str:
    lines = [
        f"Call {detail.get('call_id')} — outcome {detail.get('outcome') or 'completed'}"
    ]
    summary = detail.get("summary")
    if summary:
        try:
            parsed = json.loads(summary)
            summary = parsed.get("summary") or summary
        except (TypeError, ValueError):
            pass
        lines.append(str(summary))
    for lead in detail.get("leads") or []:
        need = [lead.get("intent") or "enquiry"]
        if lead.get("details"):
            need.append(str(lead["details"]))
        lines.append("Captured need: " + " — ".join(need))
    return "\n".join(lines)[:60000]


def _sync_one(store: Store, cipher: CredentialCipher, job: dict[str, Any]) -> None:
    org_id = str(job["organization_id"])
    call_id = str(job["call_id"])

    crm = service.load_crm_provider(store, cipher, org_id)
    if crm is None:  # disconnected after the job was queued
        _finish(store, job, STATUS_SUCCEEDED, result={"skipped": "no_crm"})
        return
    detail = store.call_detail(org_id, call_id)
    if detail is None:
        _finish(store, job, STATUS_SUCCEEDED, result={"skipped": "no_call"})
        return

    result = _load_result(job)
    leads = detail.get("leads") or []
    name = next(
        (lead.get("caller_name") for lead in leads if lead.get("caller_name")), None
    )
    phone = detail.get("from_number") or next(
        (lead.get("callback_number") for lead in leads if lead.get("callback_number")),
        None,
    )

    if not result.get("contact_id"):
        contact = crm.upsert_contact(name=name, phone=phone)
        result["contact_id"] = contact.id
        _persist(store, job, result)

    if not result.get("note_id"):
        result["note_id"] = crm.add_note(
            contact_id=str(result["contact_id"]), body=_note_body(detail)
        )
        _persist(store, job, result)

    if not result.get("tasks_done"):
        task_ids = list(result.get("task_ids") or [])
        open_leads = [
            lead for lead in leads if (lead.get("status") or "new") == "new"
        ]
        for lead in open_leads[len(task_ids) :]:
            task_ids.append(
                crm.create_task(
                    contact_id=str(result["contact_id"]),
                    title=f"Follow up: {lead.get('intent') or 'caller enquiry'}",
                    body=lead.get("details"),
                )
            )
            result["task_ids"] = task_ids
            _persist(store, job, result)
        result["tasks_done"] = True

    _finish(store, job, STATUS_SUCCEEDED, result=result)


def process_due_crm_jobs(
    store: Store,
    cipher: CredentialCipher,
    *,
    batch_size: int = 10,
    stale_lock: timedelta | None = None,
) -> int:
    """Claim and run one batch of due jobs. Returns how many were processed."""
    stale = stale_lock or timedelta(seconds=CRM_SYNC_STALE_LOCK_SECONDS)
    claimed = store.claim_crm_sync_jobs(batch_size, _utcnow() - stale)
    for job in claimed:
        try:
            _sync_one(store, cipher, job)
        except ProviderError as exc:
            _retry_or_dead(store, job, exc.message, retryable=exc.retryable)
        except Exception as exc:  # one bad job must not stall the worker
            logger.exception("crm sync job %s crashed", job.get("id"))
            _retry_or_dead(
                store, job, f"{type(exc).__name__}: {exc}", retryable=True
            )
    return len(claimed)
