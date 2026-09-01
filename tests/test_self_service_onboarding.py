"""Phase 10 tenant-authorized onboarding over the managed telephony path."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.domains.telephony.provider import ProvisioningResult

BUSINESSES = Path(__file__).resolve().parent.parent / "businesses"
PW = "correct horse staple 9"


class FakeTwilio:
    def regulatory_requirements(self, *_args, **_kwargs):
        return []

    def search_available_numbers(self, *_args, **_kwargs):
        return []

    def provision_number(self, phone_number, **_kwargs):
        return ProvisioningResult(
            account_sid="AC" + "1" * 32,
            phone_number_sid="PN" + "2" * 32,
            trunk_sid="TK" + "3" * 32,
            phone_number=phone_number,
        )


def _settings(tmp_path: Path):
    from app.settings import load_settings

    return replace(
        load_settings(),
        openai_api_key="sk-test",
        openai_webhook_secret="whsec_test",
        openai_project_id="proj_test",
        database_path=tmp_path / "calls.sqlite3",
        database_url="",
        redis_url="",
        auth_session_secret="unit-test-secret",
        environment="development",
        businesses_dir=BUSINESSES,
        app_base_url="http://testserver",
    )


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import app.main
    from app.domains.telephony.dependencies import get_twilio_provisioning_service

    monkeypatch.setattr(app.main, "settings", _settings(tmp_path))
    app.main.app.dependency_overrides[get_twilio_provisioning_service] = FakeTwilio
    try:
        with TestClient(app.main.app) as client:
            client.get("/api/v1/ping")
            yield client
    finally:
        app.main.app.dependency_overrides.pop(get_twilio_provisioning_service, None)


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["csrf"]}


def _account(
    client: TestClient, email: str, organization: str
) -> tuple[TestClient, str]:
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
    return session, response.json()["organization"]["id"]


def _configuration(number: str) -> dict:
    return {
        "slug": "self-serve-dental",
        "configuration": {
            "business": {
                "name": "Self Serve Dental",
                "timezone": "Europe/London",
                "phone_numbers": [number],
            },
            "agent": {
                "name": "Sara",
                "greeting": "Thanks for calling Self Serve Dental.",
                "voice": "marin",
            },
        },
    }


def test_owner_completes_self_service_onboarding(app_client: TestClient):
    owner, organization_id = _account(
        app_client, "owner@selfserve.example", "Self Serve Dental"
    )
    base = f"/api/v1/organizations/{organization_id}/onboarding"

    started = owner.post(base, headers=_csrf(owner))
    assert started.status_code == 201, started.text
    assert started.json()["mode"] == "self_service"
    assert started.json()["steps"]["owner"] == "accepted"
    replay = owner.post(base, headers=_csrf(owner))
    assert replay.status_code == 201
    assert replay.json()["id"] == started.json()["id"]

    number = "+15550109001"
    saved = owner.put(
        f"{base}/profile", json=_configuration(number), headers=_csrf(owner)
    )
    assert saved.status_code == 200, saved.text
    preview = owner.get(f"{base}/profile/preview")
    assert preview.status_code == 200
    assert "Self Serve Dental" in preview.json()["rendered_prompt"]

    # A tenant cannot claim arbitrary routing before the managed number exists.
    too_early = owner.post(f"{base}/publish", headers=_csrf(owner))
    assert too_early.status_code == 409
    assert too_early.json()["error"]["code"] == "telephony_provisioning_not_ready"

    provisioned = owner.post(
        f"{base}/telephony/provision",
        json={
            "idempotency_key": "self-serve-purchase-001",
            "country_code": "US",
            "number_type": "local",
            "phone_number": number,
            "purchase_approved": True,
        },
        headers=_csrf(owner),
    )
    assert provisioned.status_code == 201, provisioned.text
    assert provisioned.json()["status"] == "ready"

    published = owner.post(f"{base}/publish", headers=_csrf(owner))
    assert published.status_code == 200, published.text
    assert published.json()["steps"]["phone_number"] == "routed"
    assert published.json()["status"] == "in_progress"

    routed = app_client.app.state.business_repository.find_by_phone_number(number)
    assert routed is not None
    store = app_client.app.state.store
    store.start_call(
        organization_id,
        "self-service-test-call",
        routed.name,
        "+15550109999",
        number,
        routed.version_id,
    )
    store.finish_call(
        organization_id,
        "self-service-test-call",
        "completed",
        "Test call completed.",
        0.0,
    )
    verified = owner.post(
        f"{base}/telephony/verify-test-call",
        json={"call_id": "self-service-test-call"},
        headers=_csrf(owner),
    )
    assert verified.status_code == 200, verified.text
    assert owner.get(base).json()["status"] == "active"


def test_self_service_routes_are_role_and_tenant_scoped(app_client: TestClient):
    owner, organization_id = _account(app_client, "a@example.com", "A")
    outsider, _ = _account(app_client, "b@example.com", "B")
    base = f"/api/v1/organizations/{organization_id}/onboarding"

    assert outsider.get(base).status_code == 404
    assert outsider.post(base, headers=_csrf(outsider)).status_code == 404

    member, _ = _account(app_client, "member@example.com", "Member")
    member_id = member.get("/api/v1/me").json()["user"]["id"]
    app_client.app.state.store.add_membership(organization_id, member_id, "member")
    assert member.post(base, headers=_csrf(member)).status_code == 403
    assert owner.post(base, headers=_csrf(owner)).status_code == 201
    assert member.get(base).status_code == 403
