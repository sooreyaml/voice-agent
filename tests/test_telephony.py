"""Phase 5 shared-account Twilio onboarding API."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.domains.telephony.provider import (
    ProvisioningResult,
    TelephonyProviderError,
)

BUSINESSES = Path(__file__).resolve().parent.parent / "businesses"
PW = "correct horse staple 9"


class FakeTwilioProvisioningService:
    def __init__(self) -> None:
        self.provision_calls: list[dict] = []
        self.fail_provisioning = False

    def regulatory_requirements(self, country_code, number_type, end_user_type):
        return [
            {
                "sid": "RN" + "1" * 32,
                "friendly_name": "Business identity",
                "country_code": country_code,
                "number_type": number_type,
                "end_user_type": end_user_type,
                "requirements": {"address": "business address"},
            }
        ]

    def search_available_numbers(self, country_code, number_type, **kwargs):
        return [
            {
                "phone_number": "+15550103001",
                "friendly_name": "+1 555 010 3001",
                "country_code": country_code,
                "locality": "Boston",
                "region": "MA",
                "postal_code": None,
                "address_requirements": "none",
                "beta": False,
                "capabilities": {"voice": True, "sms": True},
            }
        ]

    def provision_number(self, phone_number, **kwargs):
        self.provision_calls.append({"phone_number": phone_number, **kwargs})
        if self.fail_provisioning:
            raise TelephonyProviderError(
                "twilio_503",
                "Temporary provider failure",
                status=503,
                retryable=True,
            )
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
        auth_session_secret="unit-test-secret",
        environment="development",
        businesses_dir=BUSINESSES,
        business_config_source="yaml",
        app_base_url="http://testserver",
        twilio_account_sid="AC" + "1" * 32,
        twilio_auth_token="unit-test-token",
    )


@pytest.fixture
def fake_twilio() -> FakeTwilioProvisioningService:
    return FakeTwilioProvisioningService()


@pytest.fixture
def app_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_twilio: FakeTwilioProvisioningService,
):
    import app.main
    from app.domains.onboarding import router as onboarding_router
    from app.domains.telephony.dependencies import get_twilio_provisioning_service

    monkeypatch.setattr(app.main, "settings", _settings(tmp_path))
    monkeypatch.setattr(onboarding_router, "deliver_invitation", lambda **_kw: None)
    app.main.app.dependency_overrides[get_twilio_provisioning_service] = lambda: (
        fake_twilio
    )
    try:
        with TestClient(app.main.app) as client:
            client.get("/api/v1/ping")
            yield client
    finally:
        app.main.app.dependency_overrides.pop(get_twilio_provisioning_service, None)


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
    admin = _account(client, "staff@platform.example", "Platform Staff")
    user_id = admin.get("/api/v1/me").json()["user"]["id"]
    client.app.state.store.set_platform_admin(user_id, True)
    return admin


def _start_onboarding(admin: TestClient, number: str) -> str:
    created = admin.post(
        "/api/v1/admin/onboarding",
        json={
            "organization_name": "Northstar Dental",
            "owner_email": "owner@northstar.example",
        },
        headers=_csrf(admin),
    )
    assert created.status_code == 201, created.text
    organization_id = created.json()["organization"]["id"]
    profile = admin.put(
        f"/api/v1/admin/onboarding/{organization_id}/profile",
        json={
            "slug": "northstar-dental",
            "configuration": {
                "business": {
                    "name": "Northstar Dental",
                    "timezone": "Europe/London",
                    "phone_numbers": [number],
                },
                "agent": {
                    "name": "Nora",
                    "greeting": "Thanks for calling Northstar.",
                    "voice": "marin",
                },
            },
        },
        headers=_csrf(admin),
    )
    assert profile.status_code == 200, profile.text
    return organization_id


def _purchase(number: str, key: str = "purchase-request-001") -> dict:
    return {
        "idempotency_key": key,
        "country_code": "US",
        "number_type": "local",
        "phone_number": number,
        "purchase_approved": True,
    }


def test_search_and_regulations_use_platform_provider(
    app_client: TestClient, fake_twilio: FakeTwilioProvisioningService
):
    admin = _platform_admin(app_client)
    organization_id = _start_onboarding(admin, "+15550103001")
    base = f"/api/v1/admin/onboarding/{organization_id}/telephony"

    requirements = admin.get(
        f"{base}/requirements",
        params={"country_code": "us", "number_type": "local"},
    )
    assert requirements.status_code == 200, requirements.text
    assert requirements.json()["items"][0]["country_code"] == "US"

    numbers = admin.get(
        f"{base}/available-numbers",
        params={"country_code": "US", "area_code": 617},
    )
    assert numbers.status_code == 200, numbers.text
    assert numbers.json()["items"][0]["phone_number"] == "+15550103001"


def test_purchase_is_idempotent_and_connects_number_to_business(
    app_client: TestClient, fake_twilio: FakeTwilioProvisioningService
):
    admin = _platform_admin(app_client)
    number = "+15550103001"
    organization_id = _start_onboarding(admin, number)
    url = f"/api/v1/admin/onboarding/{organization_id}/telephony/provision"

    first = admin.post(url, json=_purchase(number), headers=_csrf(admin))
    assert first.status_code == 201, first.text
    assert first.json()["status"] == "ready"
    assert first.json()["attempts"] == 1
    assert len(fake_twilio.provision_calls) == 1

    replay = admin.post(url, json=_purchase(number), headers=_csrf(admin))
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert len(fake_twilio.provision_calls) == 1

    phone = app_client.app.state.store.query(
        "SELECT organization_id, status, provider, provider_account_sid,"
        " provider_number_sid, provider_trunk_sid FROM phone_numbers WHERE e164 = ?",
        (number,),
    )[0]
    assert phone == {
        "organization_id": organization_id,
        "status": "inactive",
        "provider": "twilio",
        "provider_account_sid": "AC" + "1" * 32,
        "provider_number_sid": "PN" + "2" * 32,
        "provider_trunk_sid": "TK" + "3" * 32,
    }


def test_publish_waits_for_a_real_completed_test_call(
    app_client: TestClient, fake_twilio: FakeTwilioProvisioningService
):
    admin = _platform_admin(app_client)
    number = "+15550103002"
    organization_id = _start_onboarding(admin, number)
    provision_url = f"/api/v1/admin/onboarding/{organization_id}/telephony/provision"
    provisioned = admin.post(
        provision_url,
        json=_purchase(number),
        headers=_csrf(admin),
    )
    assert provisioned.status_code == 201, provisioned.text

    published = admin.post(
        f"/api/v1/admin/onboarding/{organization_id}/publish",
        headers=_csrf(admin),
    )
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "in_progress"
    assert published.json()["steps"]["phone_number"] == "routed"
    assert published.json()["steps"]["activation"] == "pending"

    verify_url = (
        f"/api/v1/admin/onboarding/{organization_id}/telephony/verify-test-call"
    )
    missing = admin.post(
        verify_url, json={"call_id": "test-call"}, headers=_csrf(admin)
    )
    assert missing.status_code == 409
    assert missing.json()["error"]["code"] == "telephony_test_call_not_found"

    routed = app_client.app.state.business_repository.find_by_phone_number(number)
    assert routed is not None
    app_client.app.state.store.start_call(
        organization_id,
        "test-call",
        routed.name,
        "+15550109999",
        number,
        routed.version_id,
    )
    app_client.app.state.store.finish_call(
        organization_id,
        "test-call",
        "resolved",
        "Successful test call.",
        0.01,
    )
    verified = admin.post(
        verify_url, json={"call_id": "test-call"}, headers=_csrf(admin)
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["status"] == "verified"
    progress = admin.get(f"/api/v1/admin/onboarding/{organization_id}").json()
    assert progress["status"] == "active"
    assert progress["steps"]["phone_number"] == "verified"
    assert progress["steps"]["activation"] == "active"


def test_failed_purchase_is_visible_and_safe_to_retry(
    app_client: TestClient, fake_twilio: FakeTwilioProvisioningService
):
    admin = _platform_admin(app_client)
    number = "+15550103003"
    organization_id = _start_onboarding(admin, number)
    base = f"/api/v1/admin/onboarding/{organization_id}/telephony"
    fake_twilio.fail_provisioning = True

    failed = admin.post(
        f"{base}/provision",
        json=_purchase(number),
        headers=_csrf(admin),
    )
    assert failed.status_code == 502
    status_response = admin.get(base)
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "failed"
    assert status_response.json()["last_error_code"] == "twilio_503"

    fake_twilio.fail_provisioning = False
    retried = admin.post(
        f"{base}/provision",
        json=_purchase(number),
        headers=_csrf(admin),
    )
    assert retried.status_code == 201, retried.text
    assert retried.json()["status"] == "ready"
    assert retried.json()["attempts"] == 2


def test_purchase_requires_admin_approval_and_selected_profile_number(
    app_client: TestClient, fake_twilio: FakeTwilioProvisioningService
):
    admin = _platform_admin(app_client)
    organization_id = _start_onboarding(admin, "+15550103004")
    url = f"/api/v1/admin/onboarding/{organization_id}/telephony/provision"

    not_approved = _purchase("+15550103004")
    not_approved["purchase_approved"] = False
    assert admin.post(url, json=not_approved, headers=_csrf(admin)).status_code == 422

    wrong_number = admin.post(
        url,
        json=_purchase("+15550103005"),
        headers=_csrf(admin),
    )
    assert wrong_number.status_code == 409
    assert wrong_number.json()["error"]["code"] == "telephony_number_not_selected"
    assert fake_twilio.provision_calls == []


def test_telephony_routes_require_platform_admin(
    app_client: TestClient, fake_twilio: FakeTwilioProvisioningService
):
    admin = _platform_admin(app_client)
    organization_id = _start_onboarding(admin, "+15550103006")
    ordinary = _account(app_client, "ordinary@example.com", "Ordinary")
    response = ordinary.get(
        f"/api/v1/admin/onboarding/{organization_id}/telephony/available-numbers",
        params={"country_code": "US"},
    )
    assert response.status_code == 403
