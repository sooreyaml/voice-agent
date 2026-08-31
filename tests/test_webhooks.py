"""Outbound webhook endpoints, signed delivery, retries/dead-letter, and replay."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.domains.webhooks import service as webhook_service
from app.domains.webhooks import signing

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
        business_config_source="yaml",
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


def _account(client: TestClient, email: str, org: str = "Acme") -> tuple[TestClient, str]:
    session = TestClient(client.app)
    session.get("/api/v1/ping")
    resp = session.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PW, "organization_name": org},
        headers=_csrf(session),
    )
    assert resp.status_code == 201, resp.text
    return session, resp.json()["organization"]["id"]


def _create_endpoint(client: TestClient, org_id: str, **body):
    payload = {"url": "https://hooks.example.test/in", **body}
    return client.post(
        f"/api/v1/organizations/{org_id}/webhook-endpoints",
        json=payload,
        headers=_csrf(client),
    )


def _mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _make_all_due(store) -> None:
    store.execute(
        "UPDATE webhook_deliveries SET next_attempt_at = ?"
        " WHERE status IN ('pending', 'failed')",
        (_now(),),
    )


# -- endpoint CRUD ---------------------------------------------------


def test_endpoint_lifecycle_and_secret_visibility(app_client: TestClient):
    owner, org_id = _account(app_client, "owner@x.com")
    created = _create_endpoint(owner, org_id, event_types=["call.completed"])
    assert created.status_code == 201
    body = created.json()
    assert body["secret"].startswith("whsec_")
    assert body["event_types"] == ["call.completed"]
    endpoint_id = body["id"]

    fetched = owner.get(
        f"/api/v1/organizations/{org_id}/webhook-endpoints/{endpoint_id}"
    ).json()
    assert "secret" not in fetched

    patched = owner.patch(
        f"/api/v1/organizations/{org_id}/webhook-endpoints/{endpoint_id}",
        json={"active": False, "event_types": None},
        headers=_csrf(owner),
    ).json()
    assert patched["active"] is False
    assert patched["event_types"] is None

    assert owner.request(
        "DELETE",
        f"/api/v1/organizations/{org_id}/webhook-endpoints/{endpoint_id}",
        headers=_csrf(owner),
    ).status_code == 204
    assert owner.get(
        f"/api/v1/organizations/{org_id}/webhook-endpoints/{endpoint_id}"
    ).status_code == 404


def test_create_endpoint_requires_admin_role(app_client: TestClient):
    _owner, org_id = _account(app_client, "owner@x.com")
    member = TestClient(app_client.app)
    member.get("/api/v1/ping")
    member.post(
        "/api/v1/auth/signup",
        json={"email": "m@x.com", "password": PW, "organization_name": "MOrg"},
        headers=_csrf(member),
    )
    member_id = member.get("/api/v1/me").json()["user"]["id"]
    app_client.app.state.store.add_membership(org_id, member_id, "member")

    assert _create_endpoint(member, org_id).status_code == 403


def test_endpoint_url_must_be_https(app_client: TestClient):
    owner, org_id = _account(app_client, "owner@x.com")
    resp = _create_endpoint(owner, org_id, url="http://insecure.example.test/in")
    assert resp.status_code == 422


def test_unknown_event_type_is_rejected(app_client: TestClient):
    owner, org_id = _account(app_client, "owner@x.com")
    resp = _create_endpoint(owner, org_id, event_types=["call.exploded"])
    assert resp.status_code == 422


def test_rotate_secret_returns_a_new_value(app_client: TestClient):
    owner, org_id = _account(app_client, "owner@x.com")
    created = _create_endpoint(owner, org_id).json()
    rotated = owner.post(
        f"/api/v1/organizations/{org_id}/webhook-endpoints/{created['id']}/rotate-secret",
        headers=_csrf(owner),
    )
    assert rotated.status_code == 200
    assert rotated.json()["secret"] != created["secret"]


def test_endpoints_are_tenant_scoped(app_client: TestClient):
    _owner, org_id = _account(app_client, "owner@x.com")
    other, _other_org = _account(app_client, "other@x.com", "Other")
    assert other.get(
        f"/api/v1/organizations/{org_id}/webhook-endpoints"
    ).status_code == 404


# -- event fan-out --------------------------------------------------


def test_emit_targets_only_active_subscribed_endpoints(app_client: TestClient):
    owner, org_id = _account(app_client, "owner@x.com")
    _create_endpoint(owner, org_id, event_types=["call.completed"])
    _create_endpoint(owner, org_id, event_types=["lead.created"])
    _create_endpoint(owner, org_id, active=False)  # subscribes all, but disabled
    store = app_client.app.state.store

    event_id = webhook_service.emit_event(
        store,
        organization_id=org_id,
        event_type="call.completed",
        dedupe_key="call-1",
        data={"call_id": "call-1"},
    )
    assert event_id is not None
    rows = store.query(
        "SELECT count(*) AS n FROM webhook_deliveries WHERE webhook_event_id = ?",
        (event_id,),
    )
    assert rows[0]["n"] == 1


def test_emit_is_idempotent_per_dedupe_key(app_client: TestClient):
    owner, org_id = _account(app_client, "owner@x.com")
    _create_endpoint(owner, org_id)
    store = app_client.app.state.store

    first = webhook_service.emit_event(
        store, organization_id=org_id, event_type="lead.created",
        dedupe_key="lead:7", data={"lead_id": 7},
    )
    second = webhook_service.emit_event(
        store, organization_id=org_id, event_type="lead.created",
        dedupe_key="lead:7", data={"lead_id": 7},
    )
    assert first == second
    assert store.query("SELECT count(*) AS n FROM webhook_events")[0]["n"] == 1
    assert store.query("SELECT count(*) AS n FROM webhook_deliveries")[0]["n"] == 1


def test_emit_without_endpoints_writes_nothing(app_client: TestClient):
    _owner, org_id = _account(app_client, "owner@x.com")
    store = app_client.app.state.store
    assert webhook_service.emit_event(
        store, organization_id=org_id, event_type="call.completed",
        dedupe_key="c", data={},
    ) is None
    assert store.query("SELECT count(*) AS n FROM webhook_events")[0]["n"] == 0


# -- signed delivery via the worker -------------------------------


async def test_worker_delivers_and_signs(app_client: TestClient):
    owner, org_id = _account(app_client, "owner@x.com")
    secret = _create_endpoint(owner, org_id, event_types=["call.completed"]).json()[
        "secret"
    ]
    store = app_client.app.state.store
    webhook_service.emit_event(
        store, organization_id=org_id, event_type="call.completed",
        dedupe_key="call-1", data={"call_id": "call-1"},
    )

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["sig_ok"] = signing.verify(
            secret, request.headers["X-Callagent-Signature"],
            request.content, now=int(time.time()),
        )
        seen["bad_secret"] = signing.verify(
            "whsec_wrong", request.headers["X-Callagent-Signature"],
            request.content, now=int(time.time()),
        )
        seen["event"] = request.headers["X-Callagent-Event"]
        seen["attempt"] = request.headers["X-Callagent-Delivery-Attempt"]
        return httpx.Response(200, text="ok")

    async with _mock_client(handler) as client:
        processed = await webhook_service.process_due_deliveries(store, client)

    assert processed == 1
    assert seen["sig_ok"] is True and seen["bad_secret"] is False
    assert seen["event"] == "call.completed" and seen["attempt"] == "1"

    row = store.query("SELECT status, attempts, last_status_code FROM webhook_deliveries")[0]
    assert row["status"] == "succeeded" and row["attempts"] == 1
    assert row["last_status_code"] == 200


async def test_worker_retries_then_dead_letters(app_client: TestClient):
    owner, org_id = _account(app_client, "owner@x.com")
    _create_endpoint(owner, org_id, event_types=["call.completed"])
    store = app_client.app.state.store
    webhook_service.emit_event(
        store, organization_id=org_id, event_type="call.completed",
        dedupe_key="call-1", data={}, max_attempts=2,
    )

    async def run_once() -> dict:
        _make_all_due(store)
        async with _mock_client(lambda r: httpx.Response(503, text="down")) as client:
            await webhook_service.process_due_deliveries(store, client)
        return store.query(
            "SELECT status, attempts, next_attempt_at FROM webhook_deliveries"
        )[0]

    after_first = await run_once()
    assert after_first["status"] == "failed" and after_first["attempts"] == 1
    assert after_first["next_attempt_at"] is not None

    after_second = await run_once()
    assert after_second["status"] == "dead" and after_second["attempts"] == 2

    attempts = store.query(
        "SELECT count(*) AS n FROM webhook_delivery_attempts"
    )[0]["n"]
    assert attempts == 2


async def test_stale_locked_delivery_is_reclaimed(app_client: TestClient):
    owner, org_id = _account(app_client, "owner@x.com")
    _create_endpoint(owner, org_id, event_types=["call.completed"])
    store = app_client.app.state.store
    webhook_service.emit_event(
        store, organization_id=org_id, event_type="call.completed",
        dedupe_key="call-1", data={},
    )
    # Simulate a worker that claimed the row and then died.
    store.execute(
        "UPDATE webhook_deliveries SET status = 'delivering', locked_at = ?",
        (_now() - timedelta(hours=1),),
    )
    async with _mock_client(lambda r: httpx.Response(200)) as client:
        processed = await webhook_service.process_due_deliveries(
            store, client, stale_lock=timedelta(minutes=5)
        )
    assert processed == 1
    assert store.query("SELECT status FROM webhook_deliveries")[0]["status"] == "succeeded"


# -- history + manual replay ------------------------------------


async def test_replay_requeues_and_needs_admin(app_client: TestClient):
    owner, org_id = _account(app_client, "owner@x.com")
    endpoint_id = _create_endpoint(
        owner, org_id, event_types=["call.completed"]
    ).json()["id"]
    store = app_client.app.state.store
    webhook_service.emit_event(
        store, organization_id=org_id, event_type="call.completed",
        dedupe_key="call-1", data={}, max_attempts=1,
    )
    _make_all_due(store)
    async with _mock_client(lambda r: httpx.Response(500)) as client:
        await webhook_service.process_due_deliveries(store, client)

    delivery_id = store.query("SELECT id FROM webhook_deliveries")[0]["id"]
    assert store.query("SELECT status FROM webhook_deliveries")[0]["status"] == "dead"

    # a plain member cannot replay
    member = TestClient(app_client.app)
    member.get("/api/v1/ping")
    member.post(
        "/api/v1/auth/signup",
        json={"email": "m@x.com", "password": PW, "organization_name": "M"},
        headers=_csrf(member),
    )
    store.add_membership(org_id, member.get("/api/v1/me").json()["user"]["id"], "member")
    assert member.post(
        f"/api/v1/organizations/{org_id}/webhook-deliveries/{delivery_id}/retry",
        headers=_csrf(member),
    ).status_code == 403

    replayed = owner.post(
        f"/api/v1/organizations/{org_id}/webhook-deliveries/{delivery_id}/retry",
        headers=_csrf(owner),
    )
    assert replayed.status_code == 202
    assert replayed.json()["status"] == "pending"
    assert replayed.json()["attempts"] == 0

    detail = owner.get(
        f"/api/v1/organizations/{org_id}/webhook-deliveries/{delivery_id}"
    ).json()
    assert len(detail["history"]) == 1
    assert detail["payload"]["type"] == "call.completed"

    listing = owner.get(
        f"/api/v1/organizations/{org_id}/webhook-endpoints/{endpoint_id}/deliveries"
    )
    assert listing.status_code == 200 and len(listing.json()["items"]) == 1
