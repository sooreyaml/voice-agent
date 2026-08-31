"""The HubSpot CRM connector against a mocked transport."""

from __future__ import annotations

import json

import httpx
import pytest

from app.domains.integrations.base import CrmProviderError
from app.domains.integrations.providers.hubspot import HubSpotCrm


def _crm(handler) -> HubSpotCrm:
    def factory(**kwargs):
        kwargs.pop("transport", None)
        return httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    return HubSpotCrm("pat-na1-secret-token-value", client_factory=factory)


def test_verify_returns_portal_id():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/account-info/v3/details"
        assert request.headers["Authorization"] == "Bearer pat-na1-secret-token-value"
        return httpx.Response(200, json={"portalId": 12345678})

    assert _crm(handler).verify() == {"external_account_id": "12345678"}


def test_find_contact_searches_email_then_phone():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        seen.append(body["filterGroups"][0]["filters"][0]["propertyName"])
        if body["filterGroups"][0]["filters"][0]["propertyName"] == "phone":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "701",
                            "properties": {
                                "firstname": "Dana",
                                "lastname": "Scully",
                                "phone": "+16175550188",
                            },
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"results": []})

    contact = _crm(handler).find_contact(
        phone="+16175550188", email="dana@example.com"
    )
    assert seen == ["email", "phone"]  # email tried first, then phone
    assert contact is not None
    assert contact.id == "701" and contact.name == "Dana Scully"


def test_upsert_creates_when_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            return httpx.Response(200, json={"results": []})
        assert request.method == "POST" and request.url.path == "/crm/v3/objects/contacts"
        props = json.loads(request.read())["properties"]
        assert props["firstname"] == "Fox" and props["lastname"] == "Mulder"
        assert props["phone"] == "+15550100"
        return httpx.Response(
            201, json={"id": "999", "properties": props}
        )

    contact = _crm(handler).upsert_contact(name="Fox Mulder", phone="+15550100")
    assert contact.id == "999"


def test_add_note_associates_to_contact():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/crm/v3/objects/notes"
        captured["body"] = json.loads(request.read())
        return httpx.Response(201, json={"id": "note-1"})

    note_id = _crm(handler).add_note(contact_id="701", body="Called about pricing")
    assert note_id == "note-1"
    payload = captured["body"]
    assert payload["properties"]["hs_note_body"] == "Called about pricing"
    assoc = payload["associations"][0]
    assert assoc["to"]["id"] == "701"
    assert assoc["types"][0]["associationTypeId"] == 202


def test_create_task_sets_subject_and_association():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/crm/v3/objects/tasks"
        captured["body"] = json.loads(request.read())
        return httpx.Response(201, json={"id": "task-9"})

    task_id = _crm(handler).create_task(
        contact_id="701", title="Follow up: pricing_question", body="quote for 3 units"
    )
    assert task_id == "task-9"
    props = captured["body"]["properties"]
    assert props["hs_task_subject"] == "Follow up: pricing_question"
    assert props["hs_task_status"] == "NOT_STARTED"
    assert captured["body"]["associations"][0]["types"][0]["associationTypeId"] == 204


def test_http_errors_map_to_retryable_flag():
    crm = _crm(lambda r: httpx.Response(500, json={"message": "boom"}))
    with pytest.raises(CrmProviderError) as excinfo:
        crm.verify()
    assert excinfo.value.retryable is True

    crm = _crm(lambda r: httpx.Response(401, json={"message": "bad token"}))
    with pytest.raises(CrmProviderError) as excinfo:
        crm.verify()
    assert excinfo.value.retryable is False
    assert excinfo.value.code == "hubspot_http_401"


def test_transport_failure_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure")

    with pytest.raises(CrmProviderError) as excinfo:
        _crm(handler).verify()
    assert excinfo.value.code == "transport_error" and excinfo.value.retryable
