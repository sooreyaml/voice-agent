"""Phase 10 privacy, data-rights, spend-limit, and isolation controls."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.domains.billing.usage import insert_statement, usage_event

BUSINESSES = Path(__file__).resolve().parent.parent / "businesses"
PW = "correct horse staple 9"


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

    monkeypatch.setattr(app.main, "settings", _settings(tmp_path))
    with TestClient(app.main.app) as client:
        client.get("/api/v1/ping")
        yield client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["csrf"]}


def _account(
    client: TestClient, email: str, organization: str
) -> tuple[TestClient, str, str]:
    session = TestClient(client.app)
    session.get("/api/v1/ping")
    response = session.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PW, "organization_name": organization},
        headers=_csrf(session),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return session, body["organization"]["id"], body["organization"]["slug"]


def test_retention_settings_and_export_are_tenant_scoped(app_client: TestClient):
    owner, organization_id, _slug = _account(
        app_client, "owner@example.com", "Privacy Org"
    )
    outsider, _other_id, _ = _account(app_client, "other@example.com", "Other Org")
    privacy_url = f"/api/v1/organizations/{organization_id}/privacy"

    assert owner.get(privacy_url).json()["transcript_retention_days"] == 90
    updated = owner.patch(
        privacy_url,
        json={"transcript_retention_days": 30},
        headers=_csrf(owner),
    )
    assert updated.status_code == 200
    assert updated.json()["transcript_retention_days"] == 30
    assert outsider.get(privacy_url).status_code == 404

    store = app_client.app.state.store
    store.start_call(organization_id, "export-call", "Privacy Org", "+1", "+2")
    store.add_turn(organization_id, "export-call", "caller", "Export my call")
    store.finish_call(organization_id, "export-call", "completed", "Summary", 0.0)

    requested = owner.post(
        f"/api/v1/organizations/{organization_id}/data-requests/exports",
        json={"idempotency_key": "export-request-001"},
        headers=_csrf(owner),
    )
    assert requested.status_code == 202, requested.text
    from app.domains.privacy import service as privacy_service

    assert privacy_service.process_due_data_requests(store) == 1
    result = owner.get(
        f"/api/v1/organizations/{organization_id}/data-requests/"
        f"{requested.json()['id']}"
    )
    assert result.status_code == 200
    export = result.json()["result"]
    assert export["organization"]["id"] == organization_id
    assert export["turns"][0]["text"] == "Export my call"
    serialized = result.text
    assert "password_hash" not in serialized
    assert "token_hash" not in serialized
    assert "encrypted_credentials" not in serialized
    assert "endpoint_secret" not in serialized


def test_deletion_is_confirmed_delayed_and_cancellable(app_client: TestClient):
    owner, organization_id, slug = _account(
        app_client, "delete@example.com", "Deletion Org"
    )
    url = f"/api/v1/organizations/{organization_id}/data-requests/deletion"
    mismatch = owner.post(
        url,
        json={
            "idempotency_key": "delete-request-001",
            "confirm_organization_slug": "wrong-slug",
        },
        headers=_csrf(owner),
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "deletion_confirmation_mismatch"

    requested = owner.post(
        url,
        json={
            "idempotency_key": "delete-request-001",
            "confirm_organization_slug": slug,
        },
        headers=_csrf(owner),
    )
    assert requested.status_code == 202, requested.text
    execute_after = datetime.fromisoformat(requested.json()["execute_after"])
    created_at = datetime.fromisoformat(requested.json()["created_at"])
    assert (execute_after - created_at).days >= 6

    cancelled = owner.post(
        f"/api/v1/organizations/{organization_id}/data-requests/"
        f"{requested.json()['id']}/cancel",
        headers=_csrf(owner),
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert app_client.app.state.store.organization(organization_id) is not None


def test_monthly_spend_limit_blocks_hard_but_not_soft_limit(app_client: TestClient):
    owner, organization_id, _ = _account(app_client, "spend@example.com", "Spend Org")
    url = f"/api/v1/organizations/{organization_id}/billing/spend-limit"
    configured = owner.put(
        url,
        json={
            "monthly_limit_micros": 1_000_000,
            "hard_limit": True,
            "warning_threshold_percent": 80,
        },
        headers=_csrf(owner),
    )
    assert configured.status_code == 200, configured.text

    event = usage_event(
        organization_id=organization_id,
        event_type="reconciliation.test",
        quantity=1,
        unit="event",
        source="reconciliation",
        idempotency_key="spend-limit-test-001",
        customer_charge_micros=1_200_000,
        occurred_at=datetime.now(UTC),
    )
    app_client.app.state.store.transaction([insert_statement(event)])
    status_response = owner.get(url)
    assert status_response.status_code == 200
    assert status_response.json()["warning"] is True
    assert status_response.json()["blocked"] is True

    from app.domains.billing.services import spend

    assert spend.call_is_allowed(app_client.app.state.store, organization_id) is False
    assert spend.call_is_allowed(app_client.app.state.store, organization_id) is False
    exceeded = [
        row
        for row in app_client.app.state.store.audit_log_page(organization_id, 20)
        if row["action"] == "billing.spend_limit_exceeded"
    ]
    assert len(exceeded) == 1

    soft = owner.put(
        url,
        json={
            "monthly_limit_micros": 1_000_000,
            "hard_limit": False,
            "warning_threshold_percent": 80,
        },
        headers=_csrf(owner),
    )
    assert soft.json()["blocked"] is False
    assert soft.json()["warning"] is True
    assert spend.call_is_allowed(app_client.app.state.store, organization_id) is True
