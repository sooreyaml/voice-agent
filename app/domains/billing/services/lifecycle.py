"""Organization lifecycle jobs the worker drives.

- ``reap_abandoned_signups``: a signup that never finished checkout has its
  number returned to the pool and its agent archived after the grace window.
- ``suspend_overdue``: a past-due subscription stops answering the phone once the
  dunning grace has elapsed (reversible).
- ``restore_paid``: the mirror image, called from the Stripe ``invoice.paid``
  path.

All FastAPI-free; safe to run repeatedly (each pass only touches rows still in
the state it acts on). Routing needs BOTH an ``active`` ``phone_numbers`` row and
a ``published`` ``agent_versions`` row (see ``BusinessRepository.find_by_phone_number``),
so deactivating the number alone is enough to take an agent off the air.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from app.domains.audit.models import AuditAction
from app.store import Store

logger = logging.getLogger(__name__)

# A signup is only reapable while it is still 'provisioning' (never converted)
# and its subscription never got past these pre-payment states.
_UNPAID_SUBSCRIPTION_STATES = ("incomplete", "checkout_pending", "not_started")


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _deactivate_numbers(store: Store, organization_id: str, now: datetime) -> None:
    store.execute(
        "UPDATE phone_numbers SET status = 'inactive', updated_at = ?"
        " WHERE organization_id = ? AND status = 'active'",
        (now, organization_id),
    )


def _teardown_routing(store: Store, organization_id: str) -> None:
    """Permanent: the number is going back to the pool for another tenant, so the
    old ``phone_numbers`` rows (deterministic ids) must not linger or the next
    ``publish`` of that E.164 collides. Nothing FKs to ``phone_numbers``.
    """
    store.execute(
        "UPDATE agent_versions SET status = 'archived'"
        " WHERE organization_id = ? AND status IN ('published', 'draft')",
        (organization_id,),
    )
    store.execute(
        "DELETE FROM phone_numbers WHERE organization_id = ?",
        (organization_id,),
    )


def _release_number(
    store: Store, organization_id: str, *, quarantine_days: int, now: datetime
) -> None:
    pooled = store.pool_number_for_org(organization_id)
    if pooled is None:
        return
    ever_called = store.query(
        "SELECT 1 FROM calls WHERE organization_id = ? LIMIT 1",
        (organization_id,),
    )
    quarantine_until = now + timedelta(days=quarantine_days) if ever_called else None
    store.release_pool_number(str(pooled["e164"]), quarantine_until=quarantine_until)


def reap_abandoned_signups(
    store: Store, *, grace_hours: int, quarantine_days: int
) -> int:
    """Close organizations that signed up but never paid within the grace window."""
    now = _utcnow()
    cutoff = now - timedelta(hours=grace_hours)
    placeholders = ", ".join("?" for _ in _UNPAID_SUBSCRIPTION_STATES)
    rows = store.query(
        "SELECT o.id FROM organizations o"
        " LEFT JOIN subscriptions s ON s.organization_id = o.id"
        " WHERE o.lifecycle = 'provisioning' AND o.deleted_at IS NULL"
        " AND o.created_at < ?"
        f" AND (s.status IS NULL OR s.status IN ({placeholders}))",
        (cutoff, *_UNPAID_SUBSCRIPTION_STATES),
    )
    reaped = 0
    for row in rows:
        org_id = str(row["id"])
        _release_number(store, org_id, quarantine_days=quarantine_days, now=now)
        _teardown_routing(store, org_id)
        store.set_organization_lifecycle(org_id, "closed")
        store.record_audit(
            AuditAction.ORG_SIGNUP_REAPED.value,
            organization_id=org_id,
            target_type="organization",
            target_id=org_id,
            metadata={"reason": "checkout_not_completed", "grace_hours": grace_hours},
        )
        logger.info("reaped abandoned signup %s", org_id)
        reaped += 1
    return reaped


def suspend_overdue(store: Store, *, grace_days: int) -> int:
    """Stop routing for subscriptions that have been past due beyond the grace."""
    now = _utcnow()
    cutoff = now - timedelta(days=grace_days)
    rows = store.query(
        "SELECT o.id FROM organizations o"
        " JOIN subscriptions s ON s.organization_id = o.id"
        " WHERE o.deleted_at IS NULL"
        " AND o.lifecycle = 'active'"
        " AND s.status IN ('past_due', 'unpaid')"
        " AND s.updated_at < ?",
        (cutoff,),
    )
    suspended = 0
    for row in rows:
        org_id = str(row["id"])
        _deactivate_numbers(store, org_id, now)
        store.set_organization_lifecycle(org_id, "suspended")
        store.record_audit(
            AuditAction.ORG_SUSPENDED.value,
            organization_id=org_id,
            target_type="organization",
            target_id=org_id,
            metadata={"reason": "payment_overdue", "grace_days": grace_days},
        )
        logger.info("suspended overdue organization %s", org_id)
        suspended += 1
    return suspended


def restore_paid(store: Store, organization_id: str) -> None:
    """Re-activate a previously suspended organization after a successful payment.

    Called from the Stripe ``invoice.paid`` handler. A closed (reaped)
    organization is not resurrected.
    """
    org = store.organization(organization_id)
    if org is None or org["lifecycle"] != "suspended":
        return
    now = _utcnow()
    store.execute(
        "UPDATE phone_numbers SET status = 'active', updated_at = ?"
        " WHERE organization_id = ? AND status = 'inactive'",
        (now, organization_id),
    )
    store.set_organization_lifecycle(organization_id, "active")
    store.record_audit(
        AuditAction.ORG_RESTORED.value,
        organization_id=organization_id,
        target_type="organization",
        target_id=organization_id,
        metadata={"reason": "payment_received"},
    )
    logger.info("restored organization %s after payment", organization_id)


__all__ = [
    "reap_abandoned_signups",
    "restore_paid",
    "suspend_overdue",
]
