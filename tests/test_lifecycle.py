"""Reaper and dunning: abandoned signups close, past-due tenants stop answering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.domains.auth.service import register
from app.domains.billing.services.lifecycle import (
    reap_abandoned_signups,
    restore_paid,
    suspend_overdue,
)
from app.domains.businesses.repository import BusinessRepository
from app.store import Store

BUSINESSES = Path(__file__).resolve().parent.parent / "businesses"
TEMPLATE = BUSINESSES / "_default.yaml"
PW = "correct horse staple 9"


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _seed_plan(store: Store) -> None:
    now = _now()
    store.execute(
        "INSERT INTO billing_plans (id, code, name, status, currency,"
        " monthly_amount_minor, included_seconds,"
        " overage_amount_micros_per_second, stripe_price_id,"
        " stripe_meter_event_name, entitlements, created_at, updated_at)"
        " VALUES ('plan-1', 'starter', 'Starter', 'active', 'USD', 0, 0, 0,"
        " 'price_x', 'call_seconds', '{}', ?, ?)",
        (now, now),
    )


def _register(store: Store, email: str, name: str):
    return register(
        store,
        email=email,
        password=PW,
        organization_name=name,
        default_profile_template=TEMPLATE,
        default_timezone="America/New_York",
        billing_active=True,
        default_plan_code="starter",
    )


def test_reaper_closes_abandoned_signup_and_recycles_the_number(tmp_path: Path):
    store = Store(tmp_path / "x.sqlite3")
    _seed_plan(store)
    store.add_pool_number("+15551230001", "US")

    result = _register(store, "a@acme.test", "Acme")
    org_id = str(result.organization["id"])
    assert result.phone_number == "+15551230001"
    assert store.organization(org_id)["lifecycle"] == "provisioning"

    # Nobody finished checkout; age the signup past the grace window.
    store.execute(
        "UPDATE organizations SET created_at = ? WHERE id = ?",
        (_now() - timedelta(hours=48), org_id),
    )

    assert reap_abandoned_signups(store, grace_hours=24, quarantine_days=30) == 1
    assert store.organization(org_id)["lifecycle"] == "closed"
    # Never took a call -> straight back to available, not quarantined.
    assert store.available_pool_count() == 1
    assert BusinessRepository(store).find_by_phone_number("+15551230001") is None

    # A fresh signup can take the recycled number.
    again = _register(store, "b@acme.test", "Beta")
    assert again.phone_number == "+15551230001"


def test_reaper_leaves_a_paid_signup_alone(tmp_path: Path):
    store = Store(tmp_path / "x.sqlite3")
    _seed_plan(store)
    store.add_pool_number("+15551230002", "US")
    result = _register(store, "c@acme.test", "Gamma")
    org_id = str(result.organization["id"])

    # Checkout completed: lifecycle moved off 'provisioning', subscription live.
    store.set_organization_lifecycle(org_id, "active")
    store.execute(
        "UPDATE subscriptions SET status = 'active' WHERE organization_id = ?",
        (org_id,),
    )
    store.execute(
        "UPDATE organizations SET created_at = ? WHERE id = ?",
        (_now() - timedelta(hours=48), org_id),
    )

    assert reap_abandoned_signups(store, grace_hours=24, quarantine_days=30) == 0
    assert store.organization(org_id)["lifecycle"] == "active"


def test_dunning_suspends_then_a_payment_restores(tmp_path: Path):
    store = Store(tmp_path / "x.sqlite3")
    _seed_plan(store)
    store.add_pool_number("+15551230003", "US")
    result = _register(store, "d@acme.test", "Delta")
    org_id = str(result.organization["id"])
    store.set_organization_lifecycle(org_id, "active")
    store.execute(
        "UPDATE subscriptions SET status = 'past_due', updated_at = ?"
        " WHERE organization_id = ?",
        (_now() - timedelta(days=10), org_id),
    )

    assert suspend_overdue(store, grace_days=7) == 1
    assert store.organization(org_id)["lifecycle"] == "suspended"
    assert BusinessRepository(store).find_by_phone_number("+15551230003") is None

    restore_paid(store, org_id)
    assert store.organization(org_id)["lifecycle"] == "active"
    routed = BusinessRepository(store).find_by_phone_number("+15551230003")
    assert routed is not None and routed.name == "Delta"


def test_dunning_respects_the_grace_window(tmp_path: Path):
    store = Store(tmp_path / "x.sqlite3")
    _seed_plan(store)
    store.add_pool_number("+15551230004", "US")
    result = _register(store, "e@acme.test", "Epsilon")
    org_id = str(result.organization["id"])
    store.set_organization_lifecycle(org_id, "active")
    store.execute(
        "UPDATE subscriptions SET status = 'past_due', updated_at = ?"
        " WHERE organization_id = ?",
        (_now() - timedelta(days=2), org_id),
    )

    assert suspend_overdue(store, grace_days=7) == 0
    assert store.organization(org_id)["lifecycle"] == "active"
