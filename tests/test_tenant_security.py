"""Authorization regression matrix for Phase 10 tenant-owned controls."""

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
        redis_url="",
        auth_session_secret="unit-test-secret",
        environment="development",
        businesses_dir=BUSINESSES,
        billing_enabled=True,  # matrix covers the spend-limit routes
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


def _account(client: TestClient, email: str, org: str) -> tuple[TestClient, str]:
    session = TestClient(client.app)
    session.get("/api/v1/ping")
    response = session.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PW, "organization_name": org},
        headers=_csrf(session),
    )
    assert response.status_code == 201
    return session, response.json()["organization"]["id"]


def test_cross_tenant_phase10_reads_and_writes_are_indistinguishable_from_missing(
    app_client: TestClient,
):
    owner_a, org_a = _account(app_client, "a@example.com", "Tenant A")
    owner_b, org_b = _account(app_client, "b@example.com", "Tenant B")

    attempts = [
        owner_a.get(f"/api/v1/organizations/{org_b}/calls"),
        owner_a.get(f"/api/v1/organizations/{org_b}/webhook-endpoints"),
        owner_a.get(f"/api/v1/organizations/{org_b}/privacy"),
        owner_a.patch(
            f"/api/v1/organizations/{org_b}/privacy",
            json={"transcript_retention_days": 30},
            headers=_csrf(owner_a),
        ),
        owner_a.get(f"/api/v1/organizations/{org_b}/billing/spend-limit"),
        owner_a.put(
            f"/api/v1/organizations/{org_b}/billing/spend-limit",
            json={"monthly_limit_micros": 1_000_000},
            headers=_csrf(owner_a),
        ),
        owner_a.post(
            f"/api/v1/organizations/{org_b}/data-requests/exports",
            json={"idempotency_key": "cross-tenant-export"},
            headers=_csrf(owner_a),
        ),
    ]
    assert {response.status_code for response in attempts} == {404}
    assert all(
        response.json()["error"]["code"] == "organization_not_found"
        for response in attempts
    )
    assert owner_b.get(f"/api/v1/organizations/{org_b}/privacy").status_code == 200
    assert owner_a.get(f"/api/v1/organizations/{org_a}/privacy").status_code == 200


def test_private_management_controls_reject_scoped_api_keys(app_client: TestClient):
    owner, org_id = _account(app_client, "owner@example.com", "Tenant")
    created = owner.post(
        f"/api/v1/organizations/{org_id}/api-keys",
        json={"name": "reader", "scopes": ["calls:read", "leads:read"]},
        headers=_csrf(owner),
    )
    assert created.status_code == 201
    bearer = {"Authorization": f"Bearer {created.json()['key']}"}
    anonymous = TestClient(app_client.app)

    assert (
        anonymous.get(
            f"/api/v1/organizations/{org_id}/calls", headers=bearer
        ).status_code
        == 200
    )
    for path in (
        f"/api/v1/organizations/{org_id}/privacy",
        f"/api/v1/organizations/{org_id}/billing/spend-limit",
        f"/api/v1/organizations/{org_id}/data-requests",
        f"/api/v1/organizations/{org_id}/webhook-endpoints",
    ):
        assert anonymous.get(path, headers=bearer).status_code == 401


def test_phase10_mutations_keep_csrf_and_role_guards(app_client: TestClient):
    owner, org_id = _account(app_client, "owner@example.com", "Tenant")
    member, _ = _account(app_client, "member@example.com", "Member Workspace")
    member_id = member.get("/api/v1/me").json()["user"]["id"]
    app_client.app.state.store.add_membership(org_id, member_id, "member")

    privacy_url = f"/api/v1/organizations/{org_id}/privacy"
    assert (
        owner.patch(privacy_url, json={"transcript_retention_days": 45}).status_code
        == 403
    )
    assert (
        member.patch(
            privacy_url,
            json={"transcript_retention_days": 45},
            headers=_csrf(member),
        ).status_code
        == 403
    )
    assert (
        member.post(
            f"/api/v1/organizations/{org_id}/data-requests/exports",
            json={"idempotency_key": "member-export-request"},
            headers=_csrf(member),
        ).status_code
        == 403
    )
