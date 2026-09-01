from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.domains.auth.passwords import verify_password
from app.domains.businesses.repository import BusinessRepository
from app.domains.organizations.seeding import OrganizationSeed, seed_organization
from app.store import Store

TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "businesses"
    / "harborview-dental.yaml"
)


def _seed(**overrides: object) -> OrganizationSeed:
    values: dict[str, object] = {
        "organization_name": "Acme Dental Group",
        "organization_slug": "acme-dental",
        "owner_email": "OWNER@acme.example",
        "business_name": "Acme Dental",
        "business_slug": "acme-dental",
        "phone_numbers": ("+442071234567",),
        "timezone": "Europe/London",
        "agent_name": "Maya",
        "business_description": "A private family dental practice.",
        "contact_email": "hello@acme.example",
    }
    values.update(overrides)
    return OrganizationSeed(**values)  # type: ignore[arg-type]


def test_seed_creates_complete_account_from_template_and_is_idempotent(
    tmp_path: Path,
):
    store = Store(tmp_path / "seed.sqlite3")
    first = seed_organization(
        store,
        template_path=TEMPLATE,
        seed=_seed(),
        owner_password="correct horse staple 9",
    )

    assert first.organization_created is True
    assert first.owner_created is True
    assert first.owner_password_set is True
    assert first.profile_version_created is True
    assert store.membership_role(first.organization_id, first.owner_user_id) == "owner"

    owner = store.get_user(first.owner_user_id)
    assert owner is not None
    assert owner["email"] == "owner@acme.example"
    assert owner["email_verified_at"] is not None
    assert verify_password("correct horse staple 9", owner["password_hash"])

    onboarding = store.onboarding_record(first.organization_id)
    assert onboarding is not None
    assert onboarding["status"] == "active"
    assert onboarding["mode"] == "self_service"

    profile = BusinessRepository(store).find_by_phone_number("+44 20 7123 4567")
    assert profile is not None
    assert profile.name == "Acme Dental"
    assert profile.agent_name == "Maya"
    assert profile.raw["business"]["what_we_do"] == (
        "A private family dental practice."
    )
    assert profile.raw["contact"]["email"] == "hello@acme.example"
    assert "website" not in profile.raw["contact"]
    # Non-identity content is inherited from the canonical YAML template.
    assert profile.raw["services"][0]["name"] == "New patient exam and x-rays"

    audit_count = len(store.audit_log_page(first.organization_id, 100))
    password_hash = owner["password_hash"]
    second = seed_organization(
        store,
        template_path=TEMPLATE,
        seed=_seed(),
        owner_password="a different valid password",
    )

    assert second.organization_id == first.organization_id
    assert second.owner_user_id == first.owner_user_id
    assert second.version_id == first.version_id
    assert second.organization_created is False
    assert second.owner_created is False
    assert second.owner_password_set is False
    assert second.profile_version_created is False
    assert store.get_user(first.owner_user_id)["password_hash"] == password_hash
    assert len(store.audit_log_page(first.organization_id, 100)) == audit_count
    store.close()


def test_seed_publishes_a_new_immutable_version_when_profile_changes(tmp_path: Path):
    store = Store(tmp_path / "seed.sqlite3")
    first = seed_organization(
        store,
        template_path=TEMPLATE,
        seed=_seed(),
        owner_password="correct horse staple 9",
    )
    second = seed_organization(
        store,
        template_path=TEMPLATE,
        seed=_seed(business_description="A specialist dental practice."),
        owner_password="correct horse staple 9",
    )

    assert second.profile_version_created is True
    assert second.version_id != first.version_id
    assert second.version_number == first.version_number + 1
    store.close()


def test_seed_validates_template_overrides_before_writing(tmp_path: Path):
    store = Store(tmp_path / "seed.sqlite3")
    with pytest.raises(ValueError):
        seed_organization(
            store,
            template_path=TEMPLATE,
            seed=_seed(phone_numbers=("not-a-phone-number",)),
            owner_password="correct horse staple 9",
        )
    assert store.organization_id_for_slug("acme-dental") is None
    store.close()


def test_seed_endpoint_uses_dedicated_bearer_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import app.main
    from app.settings import settings as real_settings

    monkeypatch.setattr(
        app.main,
        "settings",
        replace(
            real_settings,
            openai_api_key="sk-test",
            openai_webhook_secret="whsec_test",
            database_path=tmp_path / "calls.sqlite3",
            database_url="",
            businesses_dir=TEMPLATE.parent,
            seed_api_token="seed-token-for-tests",
        ),
    )
    payload = {
        "organization_name": "Acme Dental Group",
        "organization_slug": "acme-dental",
        "owner_email": "owner@acme.example",
        "owner_password": "correct horse staple 9",
        "business_name": "Acme Dental",
        "phone_number": "+442071234567",
        "business_description": "A private family dental practice.",
    }

    with TestClient(app.main.app) as client:
        invalid = client.post(
            "/api/v1/operations/seed-organization",
            json=payload,
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert invalid.status_code == 401
        assert invalid.json()["error"]["code"] == "invalid_seed_token"

        created = client.post(
            "/api/v1/operations/seed-organization",
            json=payload,
            headers={"Authorization": "Bearer seed-token-for-tests"},
        )
        assert created.status_code == 200
        assert created.json()["organization"]["created"] is True
        assert created.json()["owner"]["role"] == "owner"
        assert created.json()["business_profile"]["status"] == "published"
        assert "owner_password" not in created.text

        repeated = client.post(
            "/api/v1/operations/seed-organization",
            json=payload,
            headers={"Authorization": "Bearer seed-token-for-tests"},
        )
        assert repeated.status_code == 200
        assert repeated.json()["organization"]["created"] is False
        assert repeated.json()["business_profile"]["version_created"] is False

        health = client.get("/health").json()
        assert [business["slug"] for business in health["businesses"]] == [
            "acme-dental"
        ]
