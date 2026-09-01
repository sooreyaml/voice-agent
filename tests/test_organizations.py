"""Organization CRUD, membership RBAC, invitations, audit log, platform admin."""

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
    from app.domains.organizations import router

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


def _account(client: TestClient, email: str, org: str = "Org") -> TestClient:
    """A fresh client logged in as a brand-new account + its first org."""
    session = TestClient(client.app)
    session.get("/api/v1/ping")
    resp = session.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PW, "organization_name": org},
        headers=_csrf(session),
    )
    assert resp.status_code == 201, resp.text
    return session


def _me(client: TestClient) -> dict:
    return client.get("/api/v1/me").json()


def _member_id(client: TestClient, org_id: str, email: str) -> str:
    members = client.get(f"/api/v1/organizations/{org_id}/members").json()
    return next(m["user_id"] for m in members if m["email"] == email)


def _delete(client: TestClient, url: str):
    return client.request("DELETE", url, headers=_csrf(client))


# -- organization profile ----------------------------------------------


def test_create_organization_makes_creator_owner(app_client: TestClient):
    owner = _account(app_client, "founder@x.com", "First")
    created = owner.post(
        "/api/v1/organizations", json={"name": "Second"}, headers=_csrf(owner)
    )
    assert created.status_code == 201
    orgs = {o["name"]: o["role"] for o in owner.get("/api/v1/organizations").json()}
    assert orgs == {"First": "owner", "Second": "owner"}


def test_rename_requires_admin_or_owner(app_client: TestClient, invitations: list[dict]):
    owner = _account(app_client, "owner@x.com", "Acme")
    org_id = _me(owner)["organizations"][0]["id"]

    viewer = _account(app_client, "viewer@x.com", "ViewerOrg")
    owner.post(
        f"/api/v1/organizations/{org_id}/invitations",
        json={"email": "viewer@x.com", "role": "viewer"},
        headers=_csrf(owner),
    )
    token = invitations[-1]["raw_token"]
    viewer.post(f"/api/v1/invitations/{token}/accept", json={"token": token}, headers=_csrf(viewer))

    assert viewer.patch(
        f"/api/v1/organizations/{org_id}", json={"name": "Nope"}, headers=_csrf(viewer)
    ).status_code == 403
    ok = owner.patch(
        f"/api/v1/organizations/{org_id}", json={"name": "Acme Inc"}, headers=_csrf(owner)
    )
    assert ok.status_code == 200 and ok.json()["name"] == "Acme Inc"


def test_get_organization_is_404_for_non_member(app_client: TestClient):
    owner = _account(app_client, "a@x.com", "Alpha")
    org_id = _me(owner)["organizations"][0]["id"]
    stranger = _account(app_client, "b@x.com", "Beta")
    assert stranger.get(f"/api/v1/organizations/{org_id}").status_code == 404


# -- invitations -------------------------------------------------------


def test_invite_preview_and_accept(app_client: TestClient, invitations: list[dict]):
    owner = _account(app_client, "owner@x.com", "Acme")
    org_id = _me(owner)["organizations"][0]["id"]

    created = owner.post(
        f"/api/v1/organizations/{org_id}/invitations",
        json={"email": "bob@x.com", "role": "member"},
        headers=_csrf(owner),
    )
    assert created.status_code == 201
    token = created.json()["token"]
    assert invitations[-1]["email"] == "bob@x.com"

    preview = TestClient(app_client.app).get(f"/api/v1/invitations/{token}")
    assert preview.status_code == 200
    assert preview.json() == {
        "organization_name": "Acme",
        "email": "bob@x.com",
        "role": "member",
        "expired": False,
    }

    bob = _account(app_client, "bob@x.com", "BobOrg")
    accept = bob.post(
        f"/api/v1/invitations/{token}/accept", json={"token": token}, headers=_csrf(bob)
    )
    assert accept.status_code == 200
    assert {o["name"]: o["role"] for o in bob.get("/api/v1/organizations").json()} == {
        "Acme": "member",
        "BobOrg": "owner",
    }


def test_invitation_cannot_be_accepted_twice(app_client: TestClient, invitations: list[dict]):
    owner = _account(app_client, "owner@x.com", "Acme")
    org_id = _me(owner)["organizations"][0]["id"]
    owner.post(
        f"/api/v1/organizations/{org_id}/invitations",
        json={"email": "bob@x.com", "role": "member"},
        headers=_csrf(owner),
    )
    token = invitations[-1]["raw_token"]
    bob = _account(app_client, "bob@x.com", "BobOrg")
    assert bob.post(
        f"/api/v1/invitations/{token}/accept", json={"token": token}, headers=_csrf(bob)
    ).status_code == 200
    replay = bob.post(
        f"/api/v1/invitations/{token}/accept", json={"token": token}, headers=_csrf(bob)
    )
    assert replay.status_code == 400
    assert replay.json()["error"]["code"] == "invitation_invalid"


def test_accept_rejects_email_mismatch(app_client: TestClient, invitations: list[dict]):
    owner = _account(app_client, "owner@x.com", "Acme")
    org_id = _me(owner)["organizations"][0]["id"]
    owner.post(
        f"/api/v1/organizations/{org_id}/invitations",
        json={"email": "invited@x.com", "role": "member"},
        headers=_csrf(owner),
    )
    token = invitations[-1]["raw_token"]
    intruder = _account(app_client, "someone-else@x.com", "IntruderOrg")
    resp = intruder.post(
        f"/api/v1/invitations/{token}/accept", json={"token": token}, headers=_csrf(intruder)
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "invitation_email_mismatch"


def test_reinviting_replaces_the_pending_invitation(
    app_client: TestClient, invitations: list[dict]
):
    owner = _account(app_client, "owner@x.com", "Acme")
    org_id = _me(owner)["organizations"][0]["id"]
    for _ in range(2):
        owner.post(
            f"/api/v1/organizations/{org_id}/invitations",
            json={"email": "bob@x.com", "role": "member"},
            headers=_csrf(owner),
        )
    stale, fresh = invitations[-2]["raw_token"], invitations[-1]["raw_token"]

    pending = owner.get(f"/api/v1/organizations/{org_id}/invitations").json()
    assert len(pending) == 1
    assert TestClient(app_client.app).get(f"/api/v1/invitations/{stale}").status_code == 400
    assert TestClient(app_client.app).get(f"/api/v1/invitations/{fresh}").status_code == 200


def test_member_cannot_invite_and_admin_cannot_grant_owner(
    app_client: TestClient, invitations: list[dict]
):
    owner = _account(app_client, "owner@x.com", "Acme")
    org_id = _me(owner)["organizations"][0]["id"]

    admin = _account(app_client, "admin@x.com", "AdminOrg")
    owner.post(
        f"/api/v1/organizations/{org_id}/invitations",
        json={"email": "admin@x.com", "role": "admin"},
        headers=_csrf(owner),
    )
    token = invitations[-1]["raw_token"]
    admin.post(f"/api/v1/invitations/{token}/accept", json={"token": token}, headers=_csrf(admin))

    # admin may invite lower/equal, but not owner
    assert admin.post(
        f"/api/v1/organizations/{org_id}/invitations",
        json={"email": "junior@x.com", "role": "member"},
        headers=_csrf(admin),
    ).status_code == 201
    too_high = admin.post(
        f"/api/v1/organizations/{org_id}/invitations",
        json={"email": "coowner@x.com", "role": "owner"},
        headers=_csrf(admin),
    )
    assert too_high.status_code == 403
    assert too_high.json()["error"]["code"] == "role_too_high"


def test_existing_member_cannot_be_reinvited(
    app_client: TestClient, invitations: list[dict]
):
    owner = _account(app_client, "owner@x.com", "Acme")
    org_id = _me(owner)["organizations"][0]["id"]
    owner.post(
        f"/api/v1/organizations/{org_id}/invitations",
        json={"email": "bob@x.com", "role": "member"},
        headers=_csrf(owner),
    )
    token = invitations[-1]["raw_token"]
    bob = _account(app_client, "bob@x.com", "BobOrg")
    bob.post(f"/api/v1/invitations/{token}/accept", json={"token": token}, headers=_csrf(bob))

    resp = owner.post(
        f"/api/v1/organizations/{org_id}/invitations",
        json={"email": "bob@x.com", "role": "admin"},
        headers=_csrf(owner),
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "already_member"


def test_revoke_pending_invitation(app_client: TestClient, invitations: list[dict]):
    owner = _account(app_client, "owner@x.com", "Acme")
    org_id = _me(owner)["organizations"][0]["id"]
    created = owner.post(
        f"/api/v1/organizations/{org_id}/invitations",
        json={"email": "bob@x.com", "role": "member"},
        headers=_csrf(owner),
    ).json()
    assert _delete(
        owner, f"/api/v1/organizations/{org_id}/invitations/{created['id']}"
    ).status_code == 204
    assert owner.get(f"/api/v1/organizations/{org_id}/invitations").json() == []
    assert TestClient(app_client.app).get(
        f"/api/v1/invitations/{created['token']}"
    ).status_code == 400


# -- member management ----------------------------------------------


def _seat_member(app_client, owner, org_id, invitations, email, role="member"):
    owner.post(
        f"/api/v1/organizations/{org_id}/invitations",
        json={"email": email, "role": role},
        headers=_csrf(owner),
    )
    token = invitations[-1]["raw_token"]
    session = _account(app_client, email, f"{email}-org")
    session.post(
        f"/api/v1/invitations/{token}/accept", json={"token": token}, headers=_csrf(session)
    )
    return session


def test_owner_changes_role_and_it_is_audited(
    app_client: TestClient, invitations: list[dict]
):
    owner = _account(app_client, "owner@x.com", "Acme")
    org_id = _me(owner)["organizations"][0]["id"]
    _seat_member(app_client, owner, org_id, invitations, "bob@x.com")
    bob_id = _member_id(owner, org_id, "bob@x.com")

    resp = owner.patch(
        f"/api/v1/organizations/{org_id}/members/{bob_id}",
        json={"role": "admin"},
        headers=_csrf(owner),
    )
    assert resp.status_code == 200
    assert {m["email"]: m["role"] for m in resp.json()}["bob@x.com"] == "admin"

    actions = [
        e["action"]
        for e in owner.get(f"/api/v1/organizations/{org_id}/audit-log").json()["items"]
    ]
    assert "member.role_changed" in actions


def test_non_owner_cannot_change_roles(app_client: TestClient, invitations: list[dict]):
    owner = _account(app_client, "owner@x.com", "Acme")
    org_id = _me(owner)["organizations"][0]["id"]
    admin = _seat_member(app_client, owner, org_id, invitations, "admin@x.com", "admin")
    victim_id = _member_id(owner, org_id, "owner@x.com")
    assert admin.patch(
        f"/api/v1/organizations/{org_id}/members/{victim_id}",
        json={"role": "viewer"},
        headers=_csrf(admin),
    ).status_code == 403


def test_owner_cannot_change_or_lose_own_role(app_client: TestClient):
    owner = _account(app_client, "owner@x.com", "Acme")
    org_id = _me(owner)["organizations"][0]["id"]
    owner_id = _me(owner)["user"]["id"]

    same = owner.patch(
        f"/api/v1/organizations/{org_id}/members/{owner_id}",
        json={"role": "admin"},
        headers=_csrf(owner),
    )
    assert same.status_code == 409
    assert same.json()["error"]["code"] == "cannot_change_own_role"

    leave = _delete(owner, f"/api/v1/organizations/{org_id}/members/{owner_id}")
    assert leave.status_code == 409
    assert leave.json()["error"]["code"] == "last_owner"


def test_member_can_leave_and_owner_can_remove(
    app_client: TestClient, invitations: list[dict]
):
    owner = _account(app_client, "owner@x.com", "Acme")
    org_id = _me(owner)["organizations"][0]["id"]
    bob = _seat_member(app_client, owner, org_id, invitations, "bob@x.com")
    carol = _seat_member(app_client, owner, org_id, invitations, "carol@x.com")

    bob_id = _member_id(owner, org_id, "bob@x.com")
    carol_id = _member_id(owner, org_id, "carol@x.com")

    # carol removes herself
    assert _delete(carol, f"/api/v1/organizations/{org_id}/members/{carol_id}").status_code == 204
    # bob cannot remove owner
    owner_id = _member_id(owner, org_id, "owner@x.com")
    assert _delete(bob, f"/api/v1/organizations/{org_id}/members/{owner_id}").status_code == 403
    # owner removes bob
    assert _delete(owner, f"/api/v1/organizations/{org_id}/members/{bob_id}").status_code == 204
    assert [m["email"] for m in owner.get(f"/api/v1/organizations/{org_id}/members").json()] == [
        "owner@x.com"
    ]


# -- audit log -----------------------------------------------------


def test_audit_log_is_admin_only_and_paginated(
    app_client: TestClient, invitations: list[dict]
):
    owner = _account(app_client, "owner@x.com", "Acme")
    org_id = _me(owner)["organizations"][0]["id"]
    member = _seat_member(app_client, owner, org_id, invitations, "bob@x.com")

    assert member.get(f"/api/v1/organizations/{org_id}/audit-log").status_code == 403

    first = owner.get(
        f"/api/v1/organizations/{org_id}/audit-log", params={"limit": 1}
    ).json()
    assert len(first["items"]) == 1 and first["page"]["has_more"] is True
    second = owner.get(
        f"/api/v1/organizations/{org_id}/audit-log",
        params={"limit": 1, "cursor": first["page"]["next_cursor"]},
    ).json()
    assert second["items"][0]["id"] < first["items"][0]["id"]


# -- platform administration -------------------------------------


def test_admin_routes_require_platform_admin(app_client: TestClient):
    user = _account(app_client, "nobody@x.com", "NobodyOrg")
    assert user.get("/api/v1/admin/organizations").status_code == 403


def test_platform_admin_sees_every_organization(app_client: TestClient):
    boss = _account(app_client, "boss@x.com", "BossCo")
    _account(app_client, "other@x.com", "OtherCo")
    app_client.app.state.store.set_platform_admin(_me(boss)["user"]["id"], True)

    listing = boss.get("/api/v1/admin/organizations").json()
    names = {o["name"] for o in listing["items"]}
    assert {"BossCo", "OtherCo"} <= names

    org_id = next(o["id"] for o in listing["items"] if o["name"] == "BossCo")
    detail = boss.get(f"/api/v1/admin/organizations/{org_id}")
    assert detail.status_code == 200
    assert [m["email"] for m in detail.json()["members"]] == ["boss@x.com"]
    assert boss.get(
        "/api/v1/admin/organizations/00000000-0000-0000-0000-000000000000"
    ).status_code == 404
