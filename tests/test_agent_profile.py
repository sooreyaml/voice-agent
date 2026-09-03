"""Self-service agent editing: draft -> preview -> publish, and its guardrails."""

from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BUSINESSES = Path(__file__).resolve().parent.parent / "businesses"
PW = "correct horse staple 9"


def _settings(tmp_path: Path, **overrides):
    from app.settings import load_settings

    base = replace(
        load_settings(),
        openai_api_key="sk-test",
        openai_webhook_secret="whsec_test",
        openai_project_id="proj_test",
        database_path=tmp_path / "calls.sqlite3",
        database_url="",
        auth_session_secret="unit-test-secret",
        integration_encryption_key="k8jx5ZBLhq5deNjiiCfCrYKexwPaYN8SkNIwN5OEcU0=",
        environment="development",
        businesses_dir=BUSINESSES,
        app_base_url="http://testserver",
        resend_api_key="",
        resend_from_email="",
        number_pool_country="US",
        stripe_price_id="",
    )
    return replace(base, **overrides)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import app.main

    monkeypatch.setattr(app.main, "settings", _settings(tmp_path))
    with TestClient(app.main.app) as test_client:
        test_client.get("/api/v1/ping")
        yield test_client


def _csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["csrf"]}


def _signup(
    client: TestClient, *, email="owner@acme.test", organization_name="Acme Co"
):
    return client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PW, "organization_name": organization_name},
        headers=_csrf(client),
    )


def _onboard(client: TestClient, number="+15550000123") -> str:
    """Signup with a pool number available; return the organization id."""
    store = client.app.state.store
    assert store.add_pool_number(number, "US") is True
    body = _signup(client).json()
    assert body["phone_number"] == number
    return str(body["organization"]["id"])


def _edited_config(published: dict, **changes) -> dict:
    """Take a published configuration and return an edit of it."""
    config = copy.deepcopy(published)
    config["agent"]["greeting"] = changes.get(
        "greeting", "Good day, Acme Dental, Alex speaking."
    )
    if "what_we_do" in changes:
        config["business"]["what_we_do"] = changes["what_we_do"]
    if "transfer_to" in changes:
        config.setdefault("contact", {})["transfer_to"] = changes["transfer_to"]
    if "timezone" in changes:
        config["business"]["timezone"] = changes["timezone"]
    return config


# -- happy path ----------------------------------------------------------


def test_signup_agent_is_visible_and_has_no_draft(client: TestClient):
    _onboard(client)

    state = client.get(f"/api/v1/organizations/{_org(client)}/agent").json()

    assert state["provisioned"] is True
    assert state["editable"] is True
    assert state["lifecycle"] == "active"
    assert state["active_phone_numbers"] == ["+15550000123"]
    assert state["draft"] is None
    assert state["published"]["configuration"]["business"]["name"] == "Acme Co"


def test_draft_then_publish_changes_the_live_prompt(client: TestClient):
    org_id = _onboard(client)
    published = client.get(f"/api/v1/organizations/{org_id}/agent").json()["published"][
        "configuration"
    ]

    draft = client.put(
        f"/api/v1/organizations/{org_id}/agent/draft",
        json=_edited_config(
            published,
            greeting="Acme Co, this is Alex.",
            what_we_do="We fit bespoke kitchens.",
            transfer_to="+15551112222",
        ),
        headers=_csrf(client),
    )
    assert draft.status_code == 200
    assert (
        draft.json()["configuration"]["agent"]["greeting"] == "Acme Co, this is Alex."
    )
    # The prompt the model receives is re-rendered from the edited config.
    assert "We fit bespoke kitchens." in draft.json()["rendered_prompt"]

    # Live routing is unchanged until publish.
    from app.domains.businesses.repository import BusinessRepository

    repo = BusinessRepository(client.app.state.store)
    live = repo.find_by_phone_number("+15550000123")
    assert live.greeting != "Acme Co, this is Alex."

    published_resp = client.post(
        f"/api/v1/organizations/{org_id}/agent/publish", headers=_csrf(client)
    )
    assert published_resp.status_code == 200

    live = repo.find_by_phone_number("+15550000123")
    assert live.greeting == "Acme Co, this is Alex."
    assert live.transfer_number == "+15551112222"
    # The pool number survived the edit round-trip.
    assert live.phone_numbers == ["+15550000123"]
    # Draft is consumed by publish.
    assert client.get(f"/api/v1/organizations/{org_id}/agent").json()["draft"] is None


def test_client_cannot_repoint_the_phone_number(client: TestClient):
    org_id = _onboard(client)
    published = client.get(f"/api/v1/organizations/{org_id}/agent").json()["published"][
        "configuration"
    ]
    tampered = _edited_config(published)
    tampered["business"]["phone_numbers"] = ["+19998887777"]

    client.put(
        f"/api/v1/organizations/{org_id}/agent/draft",
        json=tampered,
        headers=_csrf(client),
    )
    client.post(f"/api/v1/organizations/{org_id}/agent/publish", headers=_csrf(client))

    state = client.get(f"/api/v1/organizations/{org_id}/agent").json()
    assert state["published"]["configuration"]["business"]["phone_numbers"] == [
        "+15550000123"
    ]


def test_discard_draft_keeps_live_agent(client: TestClient):
    org_id = _onboard(client)
    published = client.get(f"/api/v1/organizations/{org_id}/agent").json()["published"][
        "configuration"
    ]
    client.put(
        f"/api/v1/organizations/{org_id}/agent/draft",
        json=_edited_config(published, greeting="Draft greeting"),
        headers=_csrf(client),
    )

    dropped = client.request(
        "DELETE",
        f"/api/v1/organizations/{org_id}/agent/draft",
        headers=_csrf(client),
    )
    assert dropped.status_code == 204
    assert client.get(f"/api/v1/organizations/{org_id}/agent").json()["draft"] is None


# -- unhappy path ------------------------------------------------------


def test_invalid_timezone_is_a_422(client: TestClient):
    org_id = _onboard(client)
    published = client.get(f"/api/v1/organizations/{org_id}/agent").json()["published"][
        "configuration"
    ]

    resp = client.put(
        f"/api/v1/organizations/{org_id}/agent/draft",
        json=_edited_config(published, timezone="Mars/Olympus"),
        headers=_csrf(client),
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_failed"


def test_publish_without_a_draft_is_404(client: TestClient):
    org_id = _onboard(client)
    resp = client.post(
        f"/api/v1/organizations/{org_id}/agent/publish", headers=_csrf(client)
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "agent_draft_not_found"


def test_suspended_org_cannot_edit_or_publish(client: TestClient):
    org_id = _onboard(client)
    published = client.get(f"/api/v1/organizations/{org_id}/agent").json()["published"][
        "configuration"
    ]
    client.app.state.store.set_organization_lifecycle(org_id, "suspended")

    resp = client.put(
        f"/api/v1/organizations/{org_id}/agent/draft",
        json=_edited_config(published),
        headers=_csrf(client),
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "agent_locked"

    state = client.get(f"/api/v1/organizations/{org_id}/agent").json()
    assert state["editable"] is False


def test_org_without_a_number_reports_unprovisioned(client: TestClient):
    # Signup while Twilio has no matching number: account exists, no agent.
    _signup(client)
    org_id = _org(client)

    state = client.get(f"/api/v1/organizations/{org_id}/agent").json()
    assert state["provisioned"] is False
    assert state["editable"] is False

    resp = client.put(
        f"/api/v1/organizations/{org_id}/agent/draft",
        json={
            "business": {"name": "x", "timezone": "UTC"},
            "agent": {"name": "A", "greeting": "Hi"},
        },
        headers=_csrf(client),
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "agent_not_provisioned"


def test_unprovisioned_owner_can_retry_an_on_demand_purchase(
    client: TestClient, fake_provisioning_provider
):
    _signup(client)
    org_id = _org(client)
    fake_provisioning_provider.add_available("+15550000456")

    response = client.post(
        f"/api/v1/organizations/{org_id}/agent/provision",
        headers=_csrf(client),
    )

    assert response.status_code == 200
    assert response.json()["provisioned"] is True
    assert response.json()["active_phone_numbers"] == ["+15550000456"]
    assert fake_provisioning_provider.purchased == ["+15550000456"]

    repeated = client.post(
        f"/api/v1/organizations/{org_id}/agent/provision",
        headers=_csrf(client),
    )
    assert repeated.status_code == 200
    assert repeated.json()["active_phone_numbers"] == ["+15550000456"]
    assert fake_provisioning_provider.purchased == ["+15550000456"]


def test_viewer_can_read_but_not_edit(client: TestClient):
    org_id = _onboard(client)
    published = client.get(f"/api/v1/organizations/{org_id}/agent").json()["published"][
        "configuration"
    ]
    store = client.app.state.store
    viewer_id = store.create_user("viewer@acme.test", password_hash=None)
    store.add_membership(org_id, viewer_id, "viewer")

    from app.domains.auth.service import issue_session

    raw, _ = issue_session(
        store, viewer_id, secret="unit-test-secret", user_agent=None, ip=None
    )
    viewer = TestClient(client.app)
    viewer.cookies.set("session", raw)
    viewer.get("/api/v1/ping")

    assert viewer.get(f"/api/v1/organizations/{org_id}/agent").status_code == 200
    blocked = viewer.put(
        f"/api/v1/organizations/{org_id}/agent/draft",
        json=_edited_config(published),
        headers={"X-CSRF-Token": viewer.cookies["csrf"]},
    )
    assert blocked.status_code == 403


def _org(client: TestClient) -> str:
    return client.get("/api/v1/me").json()["organizations"][0]["id"]
