"""Phase 4 platform-admin onboarding workflow."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BUSINESSES = Path(__file__).resolve().parent.parent / "businesses"
PW = "correct horse staple 9"


def _settings(tmp_path: Path):
    from app.settings import load_settings

    return replace(
        load_settings(),
        openai_api_key="sk-test",
        openai_webhook_secret="whsec_test",
        database_path=tmp_path / "calls.sqlite3",
        database_url="",
        auth_session_secret="unit-test-secret",
        environment="development",
        businesses_dir=BUSINESSES,
        app_base_url="http://testserver",
    )


@pytest.fixture
def invitations(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    from app.domains.onboarding import router

    box: list[dict] = []
    monkeypatch.setattr(router, "deliver_invitation", lambda **kw: box.append(kw))
    return box


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import app.main

    monkeypatch.setattr(app.main, "settings", _settings(tmp_path))
    with TestClient(app.main.app) as client:
        client.get("/api/v1/ping")
        yield client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["csrf"]}


def _account(client: TestClient, email: str, organization: str) -> TestClient:
    session = TestClient(client.app)
    session.get("/api/v1/ping")
    response = session.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "password": PW,
            "organization_name": organization,
        },
        headers=_csrf(session),
    )
    assert response.status_code == 201, response.text
    return session


def _platform_admin(client: TestClient) -> TestClient:
    admin = _account(client, "staff@example.com", "Platform Staff")
    user_id = admin.get("/api/v1/me").json()["user"]["id"]
    client.app.state.store.set_platform_admin(user_id, True)
    return admin


def _start(
    admin: TestClient,
    *,
    name: str = "Northstar Dental",
    email: str = "owner@northstar.example",
) -> dict:
    response = admin.post(
        "/api/v1/admin/onboarding",
        json={"organization_name": name, "owner_email": email},
        headers=_csrf(admin),
    )
    assert response.status_code == 201, response.text
    return response.json()


def _configuration(
    number: str = "+15550102030", greeting: str = "Thanks for calling Northstar."
) -> dict:
    return {
        "business": {
            "name": "Northstar Dental",
            "timezone": "Europe/London",
            "phone_numbers": [number],
            "what_we_do": "Family and cosmetic dentistry.",
        },
        "agent": {
            "name": "Nora",
            "greeting": greeting,
            "voice": "marin",
            "role": "receptionist",
        },
        "hours": {"monday": "09:00-17:00"},
        "guardrails": {"never": ["Diagnose a caller"]},
    }


def _save_profile(
    admin: TestClient,
    organization_id: str,
    *,
    number: str = "+15550102030",
    greeting: str = "Thanks for calling Northstar.",
):
    return admin.put(
        f"/api/v1/admin/onboarding/{organization_id}/profile",
        json={
            "slug": "northstar-dental",
            "configuration": _configuration(number, greeting),
        },
        headers=_csrf(admin),
    )


def test_onboarding_routes_require_platform_admin(app_client: TestClient):
    ordinary = _account(app_client, "ordinary@example.com", "Ordinary")
    assert ordinary.get("/api/v1/admin/onboarding").status_code == 403
    assert (
        ordinary.post(
            "/api/v1/admin/onboarding",
            json={"organization_name": "Nope", "owner_email": "owner@nope.example"},
            headers=_csrf(ordinary),
        ).status_code
        == 403
    )


def test_admin_starts_onboarding_and_owner_accepts_invitation(
    app_client: TestClient, invitations: list[dict]
):
    admin = _platform_admin(app_client)
    created = _start(admin)
    organization_id = created["organization"]["id"]

    assert created["status"] == "in_progress"
    assert created["steps"] == {
        "owner": "invited",
        "business_profile": "not_started",
        "phone_number": "not_started",
        "activation": "pending",
    }
    assert created["invitation_token"] == invitations[-1]["raw_token"]
    assert invitations[-1]["role"] == "owner"
    assert app_client.app.state.store.list_members(organization_id) == []

    owner = _account(app_client, "owner@northstar.example", "Owner Workspace")
    token = created["invitation_token"]
    accepted = owner.post(
        f"/api/v1/invitations/{token}/accept",
        json={"token": token},
        headers=_csrf(owner),
    )
    assert accepted.status_code == 200, accepted.text
    progress = admin.get(f"/api/v1/admin/onboarding/{organization_id}").json()
    assert progress["steps"]["owner"] == "accepted"
    assert (
        app_client.app.state.store.membership_role(
            organization_id, owner.get("/api/v1/me").json()["user"]["id"]
        )
        == "owner"
    )


def test_draft_preview_and_publish_activate_exact_routing(
    app_client: TestClient, invitations: list[dict]
):
    admin = _platform_admin(app_client)
    created = _start(admin)
    organization_id = created["organization"]["id"]
    number = "+15550102031"

    saved = _save_profile(admin, organization_id, number=number)
    assert saved.status_code == 200, saved.text
    progress = saved.json()
    assert progress["steps"]["business_profile"] == "draft"
    assert progress["steps"]["phone_number"] == "selected"
    assert progress["profile"]["draft_version"] == 1
    assert progress["profile"]["published_version"] is None
    assert app_client.app.state.business_repository.find_by_phone_number(number) is None

    preview = admin.get(f"/api/v1/admin/onboarding/{organization_id}/profile/preview")
    assert preview.status_code == 200, preview.text
    assert preview.json()["version_number"] == 1
    assert "Northstar Dental" in preview.json()["rendered_prompt"]
    assert "Nora" in preview.json()["rendered_prompt"]

    published = admin.post(
        f"/api/v1/admin/onboarding/{organization_id}/publish",
        headers=_csrf(admin),
    )
    assert published.status_code == 200, published.text
    active = published.json()
    assert active["status"] == "active"
    assert active["steps"] == {
        "owner": "invited",
        "business_profile": "published",
        "phone_number": "routed",
        "activation": "active",
    }
    assert active["profile"]["draft_version"] is None
    assert active["profile"]["published_version"] == 1
    routed = app_client.app.state.business_repository.find_by_phone_number(number)
    assert routed is not None
    assert routed.organization_id == organization_id
    assert routed.version_id == preview.json()["version_id"]
    assert routed.version_number == 1
    actions = {
        row["action"]
        for row in app_client.app.state.store.audit_log_page(organization_id, limit=20)
    }
    assert {
        "onboarding.started",
        "profile.draft_saved",
        "profile.published",
        "onboarding.activated",
    } <= actions


def test_new_draft_does_not_change_live_profile_until_republished(
    app_client: TestClient, invitations: list[dict]
):
    admin = _platform_admin(app_client)
    created = _start(admin)
    organization_id = created["organization"]["id"]
    number = "+15550102032"
    assert _save_profile(admin, organization_id, number=number).status_code == 200
    admin.post(
        f"/api/v1/admin/onboarding/{organization_id}/publish",
        headers=_csrf(admin),
    )
    live_before = app_client.app.state.business_repository.find_by_phone_number(number)
    assert live_before is not None

    changed = _save_profile(
        admin,
        organization_id,
        number=number,
        greeting="A new greeting that is still a draft.",
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["steps"]["business_profile"] == "changes_pending"
    live_during_draft = app_client.app.state.business_repository.find_by_phone_number(
        number
    )
    assert live_during_draft is not None
    assert live_during_draft.version_id == live_before.version_id
    assert live_during_draft.greeting == "Thanks for calling Northstar."

    republished = admin.post(
        f"/api/v1/admin/onboarding/{organization_id}/publish",
        headers=_csrf(admin),
    )
    assert republished.status_code == 200, republished.text
    live_after = app_client.app.state.business_repository.find_by_phone_number(number)
    assert live_after is not None
    assert live_after.version_id != live_before.version_id
    assert live_after.greeting == "A new greeting that is still a draft."


def test_phone_number_cannot_cross_onboarding_tenants(
    app_client: TestClient, invitations: list[dict]
):
    admin = _platform_admin(app_client)
    first = _start(admin)
    number = "+15550102033"
    first_id = first["organization"]["id"]
    assert _save_profile(admin, first_id, number=number).status_code == 200
    assert (
        admin.post(
            f"/api/v1/admin/onboarding/{first_id}/publish", headers=_csrf(admin)
        ).status_code
        == 200
    )

    second = _start(
        admin,
        name="Second Dental",
        email="owner@second.example",
    )
    conflict = _save_profile(admin, second["organization"]["id"], number=number)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "phone_number_conflict"


def test_profile_validation_and_missing_draft_are_safe(
    app_client: TestClient, invitations: list[dict]
):
    admin = _platform_admin(app_client)
    created = _start(admin)
    organization_id = created["organization"]["id"]

    missing = admin.post(
        f"/api/v1/admin/onboarding/{organization_id}/publish",
        headers=_csrf(admin),
    )
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "onboarding_profile_missing"

    invalid = _configuration()
    invalid["business"]["timezone"] = "Mars/Olympus"
    rejected = admin.put(
        f"/api/v1/admin/onboarding/{organization_id}/profile",
        json={"slug": "northstar-dental", "configuration": invalid},
        headers=_csrf(admin),
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "validation_failed"
    assert (
        admin.get(
            f"/api/v1/admin/onboarding/{organization_id}/profile/preview"
        ).status_code
        == 409
    )


def test_onboarding_list_is_paginated(app_client: TestClient, invitations: list[dict]):
    admin = _platform_admin(app_client)
    _start(admin, name="First Customer", email="owner@first.example")
    _start(admin, name="Second Customer", email="owner@second.example")

    first_page = admin.get("/api/v1/admin/onboarding", params={"limit": 1}).json()
    assert len(first_page["items"]) == 1
    assert first_page["page"]["has_more"] is True
    second_page = admin.get(
        "/api/v1/admin/onboarding",
        params={"limit": 1, "cursor": first_page["page"]["next_cursor"]},
    ).json()
    assert len(second_page["items"]) == 1
    assert second_page["items"][0]["id"] != first_page["items"][0]["id"]
