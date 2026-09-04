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
from app.domains.billing.services.subscriptions import create_incomplete_subscription
from app.domains.businesses.repository import BusinessRepository
from app.domains.telephony.service import provision_organization_number
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


def _intake(name: str) -> dict[str, str]:
    return {
        "legal_name": f"{name} LLC",
        "address_line1": "500 Main Street",
        "address_line2": None,
        "city": "Austin",
        "region": "TX",
        "postal_code": "78701",
        "country": "US",
        "contact_email": "owner@acme.test",
        "contact_phone": "+15125550100",
        "business_name": name,
        "timezone": "America/New_York",
        "industry": "Professional services",
        "what_you_do": "We answer inbound calls and capture leads for the team.",
    }


def _gated_signup(
    store: Store, email: str, name: str, number: str, *, paid: bool
) -> str:
    """Reach the post-checkout state of the gated flow: a profile-complete
    organization with a live number and a subscription that is either still
    ``incomplete`` (never paid) or ``active`` (paid).
    """
    reg = register(store, email=email, password=PW, organization_name=name)
    org_id = str(reg.organization["id"])
    store.upsert_organization_intake(org_id, _intake(name), completed=True)
    store.advance_organization_lifecycle(
        org_id, "profile_pending", ("registered",)
    )
    create_incomplete_subscription(store, org_id, "plan-1")
    store.advance_organization_lifecycle(org_id, "eligible", ("profile_pending",))

    store.add_pool_number(number, "US")
    provision_organization_number(
        store,
        None,
        organization_id=org_id,
        default_profile_template=TEMPLATE,
        default_timezone="America/New_York",
        country="US",
        number_type="local",
        sms_enabled=False,
        bundle_sid=None,
        address_sid=None,
        intake=store.organization_intake(org_id),
    )
    if paid:
        store.execute(
            "UPDATE subscriptions SET status = 'active' WHERE organization_id = ?",
            (org_id,),
        )
        store.advance_organization_lifecycle(org_id, "active", ("eligible",))
    else:
        store.advance_organization_lifecycle(org_id, "provisioning", ("eligible",))
    return org_id


def test_reaper_closes_abandoned_signup_and_recycles_the_number(tmp_path: Path):
    store = Store(tmp_path / "x.sqlite3")
    _seed_plan(store)

    org_id = _gated_signup(store, "a@acme.test", "Acme", "+15551230001", paid=False)
    assert store.organization(org_id)["lifecycle"] == "provisioning"
    assert store.active_phone_number(org_id) == "+15551230001"

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


def test_reaper_leaves_a_paid_signup_alone(tmp_path: Path):
    store = Store(tmp_path / "x.sqlite3")
    _seed_plan(store)
    org_id = _gated_signup(store, "c@acme.test", "Gamma", "+15551230002", paid=True)

    store.execute(
        "UPDATE organizations SET created_at = ? WHERE id = ?",
        (_now() - timedelta(hours=48), org_id),
    )

    assert reap_abandoned_signups(store, grace_hours=24, quarantine_days=30) == 0
    assert store.organization(org_id)["lifecycle"] == "active"


def test_reaper_leaves_a_form_filler_without_a_number_alone(tmp_path: Path):
    """A signup that verified and completed its profile but never paid sits in
    'eligible' with no number — it holds no billable resource, so the reaper
    closes it on grace but there is nothing to recycle."""
    store = Store(tmp_path / "x.sqlite3")
    _seed_plan(store)
    reg = register(store, email="f@acme.test", password=PW, organization_name="Zeta")
    org_id = str(reg.organization["id"])
    store.upsert_organization_intake(org_id, _intake("Zeta"), completed=True)
    store.set_organization_lifecycle(org_id, "eligible")
    create_incomplete_subscription(store, org_id, "plan-1")
    store.execute(
        "UPDATE organizations SET created_at = ? WHERE id = ?",
        (_now() - timedelta(hours=48), org_id),
    )

    assert reap_abandoned_signups(store, grace_hours=24, quarantine_days=30) == 1
    assert store.organization(org_id)["lifecycle"] == "closed"


def test_reaper_leaves_an_unverified_signup_alone(tmp_path: Path):
    """A bare 'registered'/'profile_pending' signup holds nothing and is not the
    reaper's job — it just lingers until the owner comes back."""
    store = Store(tmp_path / "x.sqlite3")
    _seed_plan(store)
    reg = register(store, email="g@acme.test", password=PW, organization_name="Eta")
    org_id = str(reg.organization["id"])
    store.execute(
        "UPDATE organizations SET created_at = ? WHERE id = ?",
        (_now() - timedelta(hours=200), org_id),
    )

    assert reap_abandoned_signups(store, grace_hours=24, quarantine_days=30) == 0
    assert store.organization(org_id)["lifecycle"] == "registered"


def test_dunning_suspends_then_a_payment_restores(tmp_path: Path):
    store = Store(tmp_path / "x.sqlite3")
    _seed_plan(store)
    org_id = _gated_signup(store, "d@acme.test", "Delta", "+15551230003", paid=True)
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
    org_id = _gated_signup(store, "e@acme.test", "Epsilon", "+15551230004", paid=True)
    store.execute(
        "UPDATE subscriptions SET status = 'past_due', updated_at = ?"
        " WHERE organization_id = ?",
        (_now() - timedelta(days=2), org_id),
    )

    assert suspend_overdue(store, grace_days=7) == 0
    assert store.organization(org_id)["lifecycle"] == "active"
